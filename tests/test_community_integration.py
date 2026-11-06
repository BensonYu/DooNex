"""Real application routing/migrations; only GitHub transport is synthetic."""

import io
import json
import tarfile
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from doonex.api import create_app
from doonex.discovery import Discovery
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from test_discovery import PublicGit


def test_community_migration_preserves_existing_private_records(store, request_data):
    run = store.create_run(request_data)
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO library_versions VALUES('skill',%s,%s)",
            ("a" * 64, Jsonb({"private": "migration sentinel"})),
        )
        before = connection.execute(
            "SELECT kind,id,data FROM library_versions ORDER BY kind,id"
        ).fetchall()
    store.initialize()
    store.initialize()
    assert store.get(run["id"]) == run
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT kind,id,data FROM library_versions ORDER BY kind,id"
            ).fetchall()
            == before
        )
        assert (
            connection.execute("SELECT count(*) n FROM community_settings").fetchone()[
                "n"
            ]
            == 0
        )


def test_community_routes_keep_host_origin_and_admin_boundaries(settings):
    with TestClient(create_app(settings)) as client:
        public = "/api/community/public/site"
        assert client.get(public).status_code == 404
        assert (
            client.get(public, headers={"Host": "untrusted.example"}).status_code == 403
        )
        assert (
            client.get(
                public, headers={"Origin": "https://untrusted.example"}
            ).status_code
            == 403
        )
        for endpoint in ("settings", "tokens", "submissions", "releases"):
            assert client.get("/api/community/admin/" + endpoint).status_code == 401
        assert client.get("/api/community/submissions").status_code == 401
        response = client.get(
            "/api/community/admin/settings",
            headers={"Authorization": "Bearer " + settings.token},
        )
        assert response.status_code == 200 and response.json()["enabled"] is False
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Referrer-Policy"] == "no-referrer"


def test_installed_publication_download_and_withdrawal_are_separate_from_workbench(
    settings, store, monkeypatch
):
    source = PublicGit()
    original = Discovery.__init__

    def synthetic(self, *args, **kwargs):
        kwargs["transport"] = source.transport
        original(self, *args, **kwargs)

    monkeypatch.setattr(Discovery, "__init__", synthetic)
    secret = "PRIVATE-PROJECT-ANSWER-LOG-SENTINEL"
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO library_versions VALUES('skill',%s,%s)",
            ("f" * 64, Jsonb({"private": secret})),
        )
    admin = {"Authorization": "Bearer " + settings.token}
    with TestClient(create_app(settings)) as client:

        def checked(method, path, body=None, headers=None):
            response = client.request(method, path, json=body, headers=headers)
            assert response.status_code == 200, response.text
            return response.json()

        current = checked("GET", "/api/community/admin/settings", headers=admin)
        checked(
            "PUT",
            "/api/community/admin/settings",
            {
                "enabled": True,
                "title": "Synthetic local integration",
                "description": "No public deployment",
                "expected_revision": current["revision"],
            },
            admin,
        )
        token = checked(
            "POST",
            "/api/community/admin/tokens",
            {
                "label": "Synthetic author",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                "max_submissions": 1,
            },
            admin,
        )
        author = {"Authorization": "Bearer " + token["token"]}
        own = checked(
            "POST",
            "/api/community/submissions",
            {
                "request_id": str(uuid4()),
                "repository": "fixture/public",
                "ref": source.commit,
                "prefix": "skills/example",
                "title": "Fixed synthetic source",
                "category": "controlled-tests",
                "author_label": "Self reported author",
            },
            author,
        )
        assert source.calls == [] and "preview" not in own
        for path in (
            "/api/settings",
            "/api/projects",
            "/api/skills",
            "/api/community/admin/settings",
        ):
            assert client.get(path, headers=author).status_code == 401
        root = "/api/community/admin/submissions/" + own["id"]
        detail = checked("GET", root, headers=admin)
        read = checked(
            "POST",
            root + "/read",
            {
                "expected_revision": detail["revision"],
                "request_id": str(uuid4()),
            },
            admin,
        )
        preview = checked(
            "POST",
            root + "/preview",
            {
                "expected_revision": read["revision"],
                "request_id": str(uuid4()),
                "candidate_id": read["discovery"]["candidates"][0]["id"],
            },
            admin,
        )
        assert checked("GET", "/api/community/public/releases") == {"releases": []}
        review = checked(
            "POST",
            root + "/review",
            {
                "expected_revision": preview["revision"],
                "preview_sha256": preview["preview"]["sha256"],
                "status": "approved",
                "reviewer_label": "Fixture maintainer",
                "note": "Full synthetic source checked",
            },
            admin,
        )
        publish = {
            "expected_revision": review["revision"],
            "preview_sha256": review["preview"]["sha256"],
            "confirm_public_source": True,
            "confirm_license": True,
            "confirm_publish": True,
        }
        assert (
            client.post(root + "/publish", json=publish, headers=author).status_code
            == 401
        )
        release = checked("POST", root + "/publish", publish, admin)
        assert release["identity_status"] == "self_reported"
        public = "/api/community/public/releases/" + release["id"]
        assert checked("GET", public) == release
        distribution_body = {"request_id": str(uuid4())}
        distribution = checked("POST", public + "/distributions", distribution_body)
        duplicate = checked("POST", public + "/distributions", distribution_body)
        assert (
            duplicate["receipt"] == distribution["receipt"]
            and duplicate["usage_token"] is None
        )
        download_path = (
            "/api/community/public/distributions/"
            + distribution["receipt"]["id"]
            + "/download"
        )
        raw = client.get(download_path)
        assert raw.status_code == 200
        with tarfile.open(fileobj=io.BytesIO(raw.content), mode="r:gz") as archive:
            for name, content in source.files.items():
                assert archive.extractfile(name).read() == content.data
                assert archive.getmember(name).mode == content.mode
        usage_body = {"event_id": str(uuid4()), "outcome": "failed"}
        usage_auth = {"Authorization": "Bearer " + distribution["usage_token"]}
        reported = checked(
            "POST", "/api/community/submissions/usage", usage_body, usage_auth
        )
        assert (
            checked("POST", "/api/community/submissions/usage", usage_body, usage_auth)
            == reported
        )
        assert reported["controller_verified"] is False
        assert client.get("/api/settings", headers=usage_auth).status_code == 401
        for path in (
            public,
            "/api/community/public/releases",
            "/api/community/public/site",
        ):
            text = client.get(path).text
            assert all(
                value not in text
                for value in (
                    secret,
                    settings.token,
                    token["token"],
                    distribution["usage_token"],
                    str(settings.home),
                )
            )
        checked(
            "POST",
            "/api/community/admin/releases/" + release["id"] + "/withdraw",
            {"reason": "End controlled test"},
            admin,
        )
        assert client.get(public).status_code == 404
        assert client.get(download_path).status_code == 404
        assert (
            client.post(
                public + "/distributions", json={"request_id": str(uuid4())}
            ).status_code
            == 404
        )
        assert (
            checked("GET", "/api/community/submissions/" + own["id"], headers=author)[
                "release_id"
            ]
            == release["id"]
        )
    assert store.list_runs() == []
    with store.connect() as connection:
        assert (
            connection.execute("SELECT count(*) n FROM library_versions").fetchone()[
                "n"
            ]
            == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) n FROM community_usage_reports"
            ).fetchone()["n"]
            == 1
        )
        assert secret in json.dumps(
            connection.execute("SELECT data FROM library_versions").fetchall()
        )

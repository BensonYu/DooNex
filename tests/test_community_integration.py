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
        shown = checked("GET", public)
        assert shown == {
            **release,
            "quality": {
                "controller_verified": False,
                "evidence_count": 0,
                "eligible_evidence_count": 0,
            },
        }
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


from test_community_evidence import product as product  # noqa: PLC0414 - pytest fixture export


def test_public_site_release_and_evidence_agree_without_private_reads(
    product, request_data, monkeypatch
):
    from doonex.community_evidence import WithdrawEvidenceInput
    from test_community_evidence import prepare, publication, synthetic_batch

    service, release, skill = product
    batch = synthetic_batch(product, request_data, monkeypatch)
    preview = prepare(service, release, skill, batch)
    published = publication(service, preview)
    path = "/api/community/public/releases/" + release["id"]
    with TestClient(create_app(service.settings)) as client:
        # Public requests must use only frozen public projections after publication.
        def forbidden(*args, **kwargs):
            raise AssertionError("Public route read private Arena/Run data")

        from doonex.arena import Arena
        from doonex.store import Store

        monkeypatch.setattr(Arena, "get", forbidden)
        monkeypatch.setattr(Arena, "detail", forbidden)
        monkeypatch.setattr(Store, "get", forbidden)
        detail = client.get(path).json()
        assert detail["quality"] == {
            "controller_verified": True,
            "evidence_count": 1,
            "eligible_evidence_count": 1,
        }
        assert (
            client.get("/api/community/public/releases").json()["releases"][0][
                "quality"
            ]
            == detail["quality"]
        )
        site = client.get("/api/community/public/site").json()
        groups = client.get("/api/community/public/leaderboard").json()["groups"]
        assert (
            site["verified_leaderboard"] == groups
            and site["leaderboard_reason"] is None
        )
        assert len(groups) == 1 and len(groups[0]["entries"]) == 1
        assert client.get("/api/community/public/evidence").status_code == 200
        usage = client.get(path + "/usage")
        assert usage.status_code == 200 and "run_id" not in usage.text
        service.withdraw(
            published["id"],
            WithdrawEvidenceInput(expected_revision=published["revision"]),
        )
        assert client.get(path).json()["quality"]["evidence_count"] == 0
        site = client.get("/api/community/public/site").json()
        assert (
            site["verified_leaderboard"] == []
            and site["leaderboard_reason"] == "no_eligible_controller_evidence"
        )

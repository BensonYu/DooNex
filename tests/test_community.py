"""Community publication boundaries with disposable PG and synthetic GitHub."""

import io
import json
import tarfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from doonex.community import (
    COMMUNITY_SCHEMA,
    Community,
    CommunityError,
    DistributionInput,
    PreviewInput,
    PublishInput,
    ReadInput,
    ReviewInput,
    SettingsInput,
    SubmissionInput,
    TokenInput,
    UsageInput,
    WithdrawInput,
    install_community_routes,
)
from doonex.workspace import FileContent
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from pydantic import ValidationError
from test_discovery import PublicGit

PRIVATE = "PRIVATE-WORKBENCH-SECRET-ANSWER-RUN-SENTINEL"


@pytest.fixture
def community(settings, store):
    with store.connect() as c:
        c.execute(COMMUNITY_SCHEMA)
        c.execute(
            "INSERT INTO library_versions VALUES('skill',%s,%s)",
            ("f" * 64, Jsonb({"private": PRIVATE})),
        )
    source = PublicGit()
    service = Community(settings, store, transport=source.transport)
    return service, source


def enabled(service):
    current = service.settings_get()
    return service.configure(
        SettingsInput(
            enabled=True,
            title="Controlled private test community",
            description="Synthetic fixtures only",
            expected_revision=current["revision"],
        )
    )


def credential(service, limit=3):
    result = service.issue_token(
        TokenInput(
            label="Synthetic contributor",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            max_submissions=limit,
        )
    )
    return result, "Bearer " + result["token"]


def submitted(service, source, authorization, **extra):
    return service.submit(
        authorization,
        SubmissionInput(
            request_id=uuid4(),
            repository="fixture/public",
            ref=source.commit,
            prefix="skills/example",
            title="Controlled public Skill",
            summary="Synthetic source; no quality claim",
            category="controlled-tests",
            author_label="Self reported fixture author",
            author_statement="Public source attribution remains in the license",
            **extra,
        ),
    )


def prepared(service, source, authorization):
    submission = submitted(service, source, authorization)
    old = service.get_submission(submission["id"])
    read = service.read(
        old["id"], ReadInput(expected_revision=old["revision"], request_id=uuid4())
    )
    return service.preview(
        read["id"],
        PreviewInput(
            expected_revision=read["revision"],
            request_id=uuid4(),
            candidate_id=read["discovery"]["candidates"][0]["id"],
        ),
    )


def approved(service, source, authorization):
    preview = prepared(service, source, authorization)
    return service.review(
        preview["id"],
        ReviewInput(
            expected_revision=preview["revision"],
            preview_sha256=preview["preview"]["sha256"],
            status="approved",
            reviewer_label="Local maintainer fixture",
            note="Synthetic source and full license checked",
        ),
    )


def published(service, source, authorization):
    review = approved(service, source, authorization)
    body = PublishInput(
        expected_revision=review["revision"],
        preview_sha256=review["preview"]["sha256"],
        confirm_public_source=True,
        confirm_license=True,
        confirm_publish=True,
    )
    return service.publish(review["id"], body), review, body


def test_default_disabled_and_no_token_or_model_side_effects(community):
    service, source = community
    assert service.settings_get()["enabled"] is False
    with pytest.raises(CommunityError, match="community_disabled"):
        service.public_site()
    with pytest.raises(CommunityError, match="community_disabled"):
        credential(service)
    assert service.tokens() == [] and source.calls == []
    assert service.store.list_runs() == []


def test_token_is_hash_only_revocable_expiring_and_scope_limited(community):
    service, source = community
    enabled(service)
    result, authorization = credential(service)
    with service.store.connect() as c:
        rows = c.execute("SELECT token_hash,data FROM community_tokens").fetchall()
    assert result["token"] not in json.dumps(rows)
    assert result["token"] not in json.dumps(service.tokens())
    own = submitted(service, source, authorization)
    other, other_auth = credential(service)
    with pytest.raises(CommunityError, match="community_submission_not_found"):
        service.get_submission(own["id"], other_auth)
    assert service.submissions(other_auth) == []
    service.revoke(result["credential"]["id"])
    with pytest.raises(CommunityError, match="community_token_inactive"):
        service.submissions(authorization)
    with service.store.connect() as c:
        row = c.execute(
            "SELECT data FROM community_tokens WHERE id=%s",
            (other["credential"]["id"],),
        ).fetchone()["data"]
        row["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        c.execute(
            "UPDATE community_tokens SET data=%s WHERE id=%s",
            (Jsonb(row), other["credential"]["id"]),
        )
    with pytest.raises(CommunityError, match="community_token_inactive"):
        service.submissions(other_auth)
    assert source.calls == []


def test_submission_idempotence_and_persistent_capacity(community):
    service, source = community
    enabled(service)
    _result, authorization = credential(service, limit=1)
    body = SubmissionInput(
        request_id=uuid4(),
        repository="fixture/public",
        ref=source.commit,
        prefix="skills/example",
        title="One",
        category="controlled",
        author_label="Fixture",
    )
    first = service.submit(authorization, body)
    assert service.submit(authorization, body) == first
    with pytest.raises(CommunityError, match="community_submission_intent_conflict"):
        service.submit(authorization, body.model_copy(update={"title": "Changed"}))
    with pytest.raises(CommunityError, match="community_token_capacity_exhausted"):
        submitted(service, source, authorization)
    restarted = Community(service.settings, service.store, transport=source.transport)
    with pytest.raises(CommunityError, match="community_token_capacity_exhausted"):
        submitted(restarted, source, authorization)
    assert len(restarted.submissions(authorization)) == 1 and source.calls == []


def test_complete_review_publish_distribution_and_self_report(community):
    service, source = community
    enabled(service)
    _result, authorization = credential(service)
    release, review, body = published(service, source, authorization)
    assert release["identity_status"] == "self_reported"
    assert release["quality"] == {"controller_verified": False, "evidence_count": 0}
    assert release["source"]["commit"] == source.commit
    assert service.publish(review["id"], body) == release
    assert service.public_site()["verified_leaderboard"] == []
    assert service.public_site()["commercial"]["enabled"] is False
    assert PRIVATE not in json.dumps(service.releases())
    assert len(service.releases()) == 1
    distribution_input = DistributionInput(request_id=uuid4())
    distribution = service.distribute(release["id"], distribution_input)
    repeated = service.distribute(release["id"], distribution_input)
    assert (
        repeated["receipt"] == distribution["receipt"]
        and repeated["usage_token"] is None
    )
    assert distribution["receipt"]["installation_verified"] is False
    raw = service.download(distribution["receipt"]["id"])
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for name, content in source.files.items():
            assert archive.extractfile(name).read() == content.data
            assert archive.getmember(name).mode == content.mode
        assert PRIVATE.encode() not in raw
    usage = UsageInput(event_id=uuid4(), outcome="used")
    token = "Bearer " + distribution["usage_token"]
    event = service.self_report(token, usage)
    assert event["source"] == "self_reported" and event["controller_verified"] is False
    assert service.self_report(token, usage) == event
    with pytest.raises(CommunityError, match="community_usage_intent_conflict"):
        service.self_report(token, usage.model_copy(update={"outcome": "failed"}))
    assert service.store.list_runs() == []
    with service.store.connect() as c:
        assert c.execute("SELECT count(*) n FROM library_versions").fetchone()["n"] == 1


def test_pending_rejected_or_tampered_snapshots_cannot_publish(community):
    service, source = community
    enabled(service)
    _result, authorization = credential(service)
    preview = prepared(service, source, authorization)
    body = PublishInput(
        expected_revision=preview["revision"],
        preview_sha256=preview["preview"]["sha256"],
        confirm_public_source=True,
        confirm_license=True,
        confirm_publish=True,
    )
    with pytest.raises(CommunityError, match="community_publish_not_approved"):
        service.publish(preview["id"], body)
    review = service.review(
        preview["id"],
        ReviewInput(
            expected_revision=preview["revision"],
            preview_sha256=preview["preview"]["sha256"],
            status="rejected",
            reviewer_label="Fixture",
        ),
    )
    with pytest.raises(CommunityError, match="community_publish_not_approved"):
        service.publish(
            review["id"],
            body.model_copy(update={"expected_revision": review["revision"]}),
        )
    review = approved(service, source, authorization)
    (service.root / review["preview"]["sha256"] / "candidate.tar.gz").write_bytes(
        b"tampered"
    )
    body = PublishInput(
        expected_revision=review["revision"],
        preview_sha256=review["preview"]["sha256"],
        confirm_public_source=True,
        confirm_license=True,
        confirm_publish=True,
    )
    with pytest.raises(CommunityError, match="community_snapshot_changed"):
        service.publish(review["id"], body)
    assert service.releases() == []


def test_publish_concurrency_and_withdrawal_block_new_downloads(community):
    service, source = community
    enabled(service)
    _result, authorization = credential(service)
    review = approved(service, source, authorization)
    body = PublishInput(
        expected_revision=review["revision"],
        preview_sha256=review["preview"]["sha256"],
        confirm_public_source=True,
        confirm_license=True,
        confirm_publish=True,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: service.publish(review["id"], body), range(2))
        )
    assert results[0]["id"] == results[1]["id"] and len(service.releases()) == 1
    distribution = service.distribute(
        results[0]["id"], DistributionInput(request_id=uuid4())
    )
    service.download(distribution["receipt"]["id"])
    service.withdraw(results[0]["id"], WithdrawInput(reason="Controlled test finished"))
    assert service.releases() == []
    with pytest.raises(CommunityError, match="community_release_not_found"):
        service.download(distribution["receipt"]["id"])
    with pytest.raises(CommunityError, match="community_release_not_found"):
        service.distribute(results[0]["id"], DistributionInput(request_id=uuid4()))
    assert service.releases(admin=True)[0]["state"] == "withdrawn"
    with service.store.connect() as c:
        value = c.execute("SELECT data FROM community_distributions").fetchone()["data"]
        assert value["downloads"] == 1 and len(value["download_admissions"]) == 1


def test_daily_limits_survive_disable_enable_and_download_replay(community):
    service, source = community
    enabled(service)
    _result, authorization = credential(service)
    release, _review, _body = published(service, source, authorization)
    distribution = service.distribute(
        release["id"], DistributionInput(request_id=uuid4())
    )
    for _ in range(3):
        service.download(distribution["receipt"]["id"])
    with pytest.raises(CommunityError, match="community_distribution_download_limit"):
        service.download(distribution["receipt"]["id"])
    with service.store.connect() as c:
        day = datetime.now(UTC).date()
        daily = c.execute(
            "SELECT data FROM community_quotas WHERE day=%s", (day,)
        ).fetchone()["data"]
        daily["counts"]["submissions"] = daily["limits"]["submissions"]["daily"]
        c.execute(
            "UPDATE community_quotas SET data=%s WHERE day=%s", (Jsonb(daily), day)
        )
    current = service.settings_get()
    service.configure(
        SettingsInput(
            enabled=False, title=current["title"], expected_revision=current["revision"]
        )
    )
    enabled(service)
    with pytest.raises(CommunityError, match="community_capacity_exhausted"):
        submitted(service, source, authorization)


def test_public_projection_never_returns_added_private_fields(community):
    service, source = community
    enabled(service)
    _result, authorization = credential(service)
    release, _review, _body = published(service, source, authorization)
    with service.store.connect() as c:
        row = c.execute(
            "SELECT data FROM community_releases WHERE id=%s", (release["id"],)
        ).fetchone()["data"]
        row["private_note"] = PRIVATE
        row["review"]["note"] = PRIVATE
        c.execute(
            "UPDATE community_releases SET data=%s WHERE id=%s",
            (Jsonb(row), release["id"]),
        )
    assert PRIVATE not in json.dumps(service.release(release["id"]))
    assert PRIVATE not in json.dumps(service.releases())


def test_source_reads_are_explicit_and_failures_are_not_replayed(community):
    service, source = community
    enabled(service)
    _result, authorization = credential(service)
    own = submitted(service, source, authorization)
    assert source.calls == []
    original = source.respond
    source.override = lambda request, value: source.response(
        {"message": "synthetic failure"}, 503
    )
    old = service.get_submission(own["id"])
    with pytest.raises(CommunityError):
        service.read(
            old["id"], ReadInput(expected_revision=old["revision"], request_id=uuid4())
        )
    count = len(source.calls)
    failed = service.get_submission(own["id"])
    assert failed["state"] == "source_unknown" and failed["operation"] is not None
    with pytest.raises(CommunityError, match="community_operation_not_replayable"):
        service.read(
            failed["id"],
            ReadInput(expected_revision=failed["revision"], request_id=uuid4()),
        )
    assert len(source.calls) == count and original is not None


def test_api_auth_surfaces_and_strict_private_id_rejection(community):
    service, _source = community
    app = FastAPI()

    def admin(authorization: str | None = Header(default=None)):
        if authorization != "Bearer local-workbench-fixture":
            raise HTTPException(401, "local admin required")

    install_community_routes(app, service.settings, service.store, [Depends(admin)])
    with TestClient(app) as client:
        assert client.get("/api/community/public/site").status_code == 404
        assert client.get("/api/community/admin/settings").status_code == 401
        assert client.get("/api/community/submissions").status_code == 401
        enabled(service)
        result, authorization = credential(service)
        assert (
            client.get(
                "/api/community/admin/settings",
                headers={"Authorization": authorization},
            ).status_code
            == 401
        )
        assert (
            client.get(
                "/api/community/admin/settings",
                headers={"Authorization": "Bearer local-workbench-fixture"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/community/admin/submissions/" + str(uuid4()) + "/publish",
                headers={"Authorization": "Bearer local-workbench-fixture"},
                json={"version_id": "f" * 64, "confirm_publish": True},
            ).status_code
            == 422
        )
        assert client.get(
            "/api/community/public/releases?path=../../credentials.json"
        ).json() == {"releases": []}
        assert (
            result["token"]
            not in client.get(
                "/api/community/admin/tokens",
                headers={"Authorization": "Bearer local-workbench-fixture"},
            ).text
        )


def test_labels_confirmations_and_expiry_have_hard_bounds():
    with pytest.raises(ValidationError):
        TokenInput(label="x" * 81, expires_at=datetime.now(UTC) + timedelta(hours=1))
    with pytest.raises(ValidationError):
        PublishInput(
            expected_revision="a" * 64,
            preview_sha256="b" * 64,
            confirm_public_source=1,
            confirm_license=True,
            confirm_publish=True,
        )
    with pytest.raises(ValidationError):
        UsageInput(event_id=uuid4(), outcome="verified_success", score=100)


def test_unknown_license_is_preview_only_and_cannot_be_reviewed_for_publication(
    community,
):
    service, _source = community
    source = PublicGit(
        files={
            "SKILL.md": FileContent(b"Public synthetic instructions", 0o644),
            "LICENSE": FileContent(b"Unknown custom terms", 0o644),
        }
    )
    service.discovery.transport = source.transport
    enabled(service)
    _token, authorization = credential(service)
    preview = prepared(service, source, authorization)
    assert preview["state"] == "preview_only"
    assert preview["preview"]["public"]["license"]["status"] == "preview_only"
    with pytest.raises(CommunityError, match="community_review_conflict"):
        service.review(
            preview["id"],
            ReviewInput(
                expected_revision=preview["revision"],
                preview_sha256=preview["preview"]["sha256"],
                status="approved",
                reviewer_label="Fixture",
            ),
        )
    assert service.releases() == []


def test_binding_interfaces_recheck_exact_bytes_and_preserve_withdrawn_history(
    community,
):
    service, source = community
    enabled(service)
    _token, authorization = credential(service)
    release, _review, _body = published(service, source, authorization)
    binding = service.release_binding(release["id"])
    assert binding["content_digest"] == release["content_digest"]
    assert PRIVATE not in json.dumps(binding)
    distribution = service.distribute(
        release["id"], DistributionInput(request_id=uuid4())
    )
    assert (
        service.distribution_binding(distribution["receipt"]["id"])["receipt"]
        == distribution["receipt"]
    )
    assert distribution["receipt"]["content_digest"] == release["content_digest"]
    service.withdraw(release["id"], WithdrawInput(reason="History test"))
    assert (
        service.release_binding(release["id"], include_withdrawn=True)["state"]
        == "withdrawn"
    )
    with pytest.raises(CommunityError, match="community_release_not_found"):
        service.release_binding(release["id"])


def test_private_library_and_run_interfaces_are_never_used_in_community_flow(
    community, monkeypatch
):
    service, source = community

    def forbidden(*args, **kwargs):
        raise AssertionError("Community may not use private Library or Run records")

    for method in ("get", "list", "bind", "import_skill", "freeze_skill"):
        monkeypatch.setattr(service.discovery.library, method, forbidden)
    monkeypatch.setattr(service.store, "get", forbidden)
    monkeypatch.setattr(service.store, "list_runs", forbidden)
    monkeypatch.setattr(service.store, "create_run", forbidden)
    enabled(service)
    _token, authorization = credential(service)
    release, _review, _body = published(service, source, authorization)
    distribution = service.distribute(
        release["id"], DistributionInput(request_id=uuid4())
    )
    assert service.download(distribution["receipt"]["id"])
    assert PRIVATE not in json.dumps(service.releases())

"""Frozen public evidence and attribution; disposable PG and synthetic inputs.

Arena classifications use a controlled adapter (the Arena pipeline has separate
real-Docker tests). Usage tests write real Evidence artifacts with a fake check
runner, then exercise the real inspector; no network, model or product DB.
"""

import copy
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from doonex.arena import Arena
from doonex.community import (
    COMMUNITY_SCHEMA,
    Community,
    CommunityError,
    DistributionInput,
)
from doonex.community_evidence import (
    COMMUNITY_EVIDENCE_SCHEMA,
    CommunityEvidence,
    PrepareEvidenceInput,
    PublicEvidence,
    PublishEvidenceInput,
    RegisterUsageInput,
    WithdrawEvidenceInput,
    install_community_evidence_routes,
)
from doonex.evidence import Evidence
from doonex.library import Library
from doonex.store import now
from doonex.worker import run_directory
from doonex.workspace import Workspace
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from pydantic import ValidationError
from test_community import credential, enabled, published
from test_discovery import PublicGit

PRIVATE = "PRIVATE-PROJECT-PROMPT-HIDDEN-ANSWER-PATH-GATEWAY-ACCOUNT-SENTINEL"


@pytest.fixture
def product(settings, store, tmp_path, monkeypatch):
    with store.connect() as c:
        c.execute(COMMUNITY_SCHEMA)
        c.execute(COMMUNITY_EVIDENCE_SCHEMA)
    upstream = PublicGit()
    community = Community(settings, store, transport=upstream.transport)
    enabled(community)
    _, token = credential(community)
    release, _, _ = published(community, upstream, token)
    source = tmp_path / "exact-skill"
    source.mkdir()
    for name, file in upstream.files.items():
        path = source / name
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_bytes(file.data)
        path.chmod(file.mode)
    library = Library(settings, store)
    record = library.import_skill(source, {"source": PRIVATE})
    library.review(record["version_id"], "approved", PRIVATE)
    binding = library.freeze_skill(record["version_id"])
    assert binding["content_digest"] == release["content_digest"]
    assert binding["version_id"] != release["id"]
    arena = Arena(settings, store)
    monkeypatch.setattr(arena, "integrity", lambda value: {"valid": True, "errors": []})
    service = CommunityEvidence(settings, store, community=community, arena=arena)
    return service, release, binding


def ledger(**changes):
    return {
        "calls": 1,
        "tokens": 10,
        "reserved_tokens": 0,
        "unknown_calls": 0,
        "cost_usd": 0.2,
        "reserved_cost_usd": 0.0,
        "metered_cost_usd": 0.2,
        "reserved_metered_cost_usd": 0.0,
        **changes,
    }


def synthetic_batch(product, request_data, monkeypatch, **changes):
    service, _, skill = product
    identifier = str(uuid4())
    request = copy.deepcopy(request_data)
    request["model"].update(
        base_url="https://" + PRIVATE + ".invalid", credential_env=PRIVATE
    )
    value = {
        "id": identifier,
        "state": "complete",
        "split": "heldout",
        "category": "controlled-tests",
        "model": request["model"],
        "run_budget": request["budget"],
        "batch_budget": {
            "max_calls": 100,
            "max_tokens": 1000000,
            "max_seconds": 3600,
            "max_cost_usd": 20,
        },
        "taskset_id": "a" * 64,
        "taskset_report_sha256": "b" * 64,
        "environment": {"image": "synthetic", "private_hint": PRIVATE},
        "tasks": [
            {"id": f"task-{index}", "title": PRIVATE, "critical": index == 0}
            for index in range(5)
        ],
        "arms": [
            {"id": "baseline", "label": PRIVATE},
            {"id": skill["version_id"], "label": PRIVATE},
        ],
        "skills": {skill["version_id"]: skill},
        "repeats": 3,
        "minimum_pass_rate": 0.8,
        "fingerprint": "c" * 64,
        "attempts": [],
        "usage": {},
        **changes,
    }
    for arm in value["arms"]:
        for task in value["tasks"]:
            for repeat in range(value["repeats"]):
                child = copy.deepcopy(request)
                child.update(
                    prompt=PRIVATE,
                    skill=value["skills"].get(arm["id"]),
                    skill_version_id=None if arm["id"] == "baseline" else arm["id"],
                    arena={
                        "evaluation_id": identifier,
                        "taskset_id": value["taskset_id"],
                        "report_sha256": value["taskset_report_sha256"],
                        "task_id": task["id"],
                        "arm_id": arm["id"],
                        "repeat": repeat,
                        "fingerprint": value["fingerprint"],
                        "verifier_digest": "d" * 64,
                    },
                )
                run = service.store.create_run(child)
                with service.store.locked(run["id"]) as (_, run):
                    run.update(
                        state="completed_verified",
                        first_started_at=now(),
                        ended_at=now(),
                        usage=ledger(),
                        evidence={"report_sha256": "e" * 64},
                        classification_evidence=PRIVATE,
                    )
                value["attempts"].append(
                    {
                        "run_id": run["id"],
                        "task_id": task["id"],
                        "arm_id": arm["id"],
                        "repeat": repeat,
                    }
                )
    value["usage"] = {
        key: sum(ledger()[key] for _ in value["attempts"]) for key in ledger()
    }
    with service.store.connect() as c:
        c.execute(
            "INSERT INTO arena_evaluations VALUES(%s,%s,%s)",
            (value["id"], value["state"], Jsonb(value)),
        )

    def classified(entry):
        run = service.store.get(entry["run_id"])
        return {
            **entry,
            "classification": run.get("test_classification", "passed"),
            "evidence_valid": run.get("test_evidence_valid", True),
            "safety_violation": False,
            "regression_failed": False,
            "duration_seconds": run.get("test_duration", 10),
            "queued_seconds": 2,
            "usage": run["usage"],
            "classification_evidence": PRIVATE,
        }

    monkeypatch.setattr(service.arena, "attempt", classified)

    def inspection(run):
        # Pure aggregation tests use an explicit controller inspection adapter;
        # the real Arena/Evidence contract is exercised by the 45-artifact test
        # and independent review regressions, without this adapter.
        valid = (run.get("evidence") or {}).get("report_sha256") == "e" * 64
        return {
            "valid": valid,
            "verified": valid,
            "report": {"run_id": run["id"], "commands": run["request"]["checks"]},
        }

    monkeypatch.setattr(service.arena, "inspect_run_evidence", inspection)
    monkeypatch.setattr(
        service.arena,
        "detail",
        lambda *a: pytest.fail("Arena.detail must never be called"),
    )
    return value


def prepare(service, release, skill, batch):
    return service.prepare(
        PrepareEvidenceInput(
            release_id=release["id"],
            evaluation_id=batch["id"],
            arm_id=skill["version_id"],
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )


def publication(service, preview):
    return service.publish(
        preview["id"],
        PublishEvidenceInput(
            expected_revision=preview["revision"],
            preview_sha256=preview["preview_sha256"],
            confirm_public_fields=True,
            confirm_publish=True,
        ),
    )


def update_batch(service, batch, **changes):
    value = service.arena.get(batch["id"])
    value.update(changes)
    with service.store.connect() as c:
        c.execute(
            "UPDATE arena_evaluations SET state=%s,data=%s WHERE id=%s",
            (value["state"], Jsonb(value), value["id"]),
        )
    return value


def test_strict_explicit_preview_and_frozen_public_whitelist(
    product, request_data, monkeypatch
):
    service, release, skill = product
    batch = synthetic_batch(product, request_data, monkeypatch)
    preview = prepare(service, release, skill, batch)
    public = preview["public"]
    assert public["eligible"] is True and public["counts"]["passed"] == 15
    assert public["batch_counts"]["passed"] == 30
    assert public["usage"]["cost_usd"] == pytest.approx(3)
    assert len(preview["public_fields"]) > 55
    assert {item["path"]: item["value"] for item in preview["public_fields"]}[
        "usage.unknown_calls"
    ] == 0
    assert PRIVATE not in json.dumps(public)
    assert all(run["run_id"] not in json.dumps(public) for run in batch["attempts"])
    assert batch["id"] not in json.dumps(public) and skill[
        "version_id"
    ] not in json.dumps(public)
    assert service.public_evidence() == [] and service.leaderboard() == {"groups": []}
    with pytest.raises(ValidationError):
        PublicEvidence.model_validate({**public, "private_raw": PRIVATE})
    with pytest.raises(ValidationError):
        PublishEvidenceInput(
            expected_revision=preview["revision"],
            preview_sha256=preview["preview_sha256"],
            confirm_public_fields=1,
            confirm_publish=True,
        )
    result = publication(service, preview)
    assert publication(service, preview) == result
    assert result["state"] == "published"
    for target, name in (
        (service.arena, "get"),
        (service.arena, "attempt"),
        (service.arena, "integrity"),
        (service.store, "get"),
        (service.community, "release_binding"),
    ):
        monkeypatch.setattr(
            target,
            name,
            lambda *a, **kw: pytest.fail("public request read private source"),
        )
    response = service.public_evidence()
    assert len(response) == 1 and response[0]["id"] == preview["id"]
    assert PRIVATE not in json.dumps(response)
    group = service.leaderboard()["groups"][0]
    assert group["recommendations"] == {
        key: [preview["id"]] for key in ("reliable", "cost", "speed")
    }


def test_exact_content_not_private_library_id_and_original_run_binding(
    product, request_data, monkeypatch
):
    service, release, skill = product
    batch = synthetic_batch(product, request_data, monkeypatch)
    run_id = next(
        a["run_id"] for a in batch["attempts"] if a["arm_id"] == skill["version_id"]
    )
    with service.store.locked(run_id) as (_, run):
        run["request"]["skill"] = None
    with pytest.raises(CommunityError, match="original_run_binding_changed"):
        prepare(service, release, skill, batch)
    failed = service.list()[0]
    assert failed["state"] == "failed" and failed["public"] is None
    assert failed["error"] == "community_evidence_inspection_failed"
    with service.store.connect() as c:
        total = c.execute("SELECT data FROM community_evidence_limits").fetchone()[
            "data"
        ]["totals"]
    assert total["preparations"] == 1


@pytest.mark.parametrize("field", ["calls", "cost_usd", "reserved_metered_cost_usd"])
def test_complete_ledger_disagreement_blocks_eligibility(
    product, request_data, monkeypatch, field
):
    service, release, skill = product
    batch = synthetic_batch(product, request_data, monkeypatch)
    usage = batch["usage"].copy()
    usage[field] += 1
    update_batch(service, batch, usage=usage)
    preview = prepare(service, release, skill, batch)
    assert preview["public"]["eligible"] is False
    publication(service, preview)
    assert service.public_evidence()[0]["eligible"] is False
    assert service.leaderboard() == {"groups": []}


@pytest.mark.parametrize("change", ["incomplete", "tuning", "integrity", "background"])
def test_original_ineligible_context_never_gets_a_leaderboard(
    product, request_data, monkeypatch, change
):
    service, release, skill = product
    batch = synthetic_batch(product, request_data, monkeypatch)
    if change == "incomplete":
        update_batch(service, batch, attempts=batch["attempts"][:-1])
    elif change == "integrity":
        monkeypatch.setattr(
            service.arena,
            "integrity",
            lambda value: {"valid": False, "errors": [PRIVATE]},
        )
    elif change == "background":
        update_batch(service, batch, background={"private": PRIVATE})
    else:
        update_batch(service, batch, split="tuning")
    preview = prepare(service, release, skill, batch)
    assert preview["public"]["eligible"] is False
    publication(service, preview)
    assert PRIVATE not in json.dumps(service.public_evidence())
    assert service.leaderboard() == {"groups": []}


def test_preview_source_mutation_fails_publication_without_replay(
    product, request_data, monkeypatch
):
    service, release, skill = product
    batch = synthetic_batch(product, request_data, monkeypatch)
    preview = prepare(service, release, skill, batch)
    with service.store.locked(batch["attempts"][0]["run_id"]) as (_, run):
        run["evidence"]["report_sha256"] = "f" * 64
    with pytest.raises(CommunityError, match="source_changed"):
        publication(service, preview)
    assert service.list()[0]["state"] == "prepared" and service.public_evidence() == []


def test_public_reads_use_dedicated_snapshot_without_private_receipt(
    product, request_data, monkeypatch
):
    service, release, skill = product
    batch = synthetic_batch(product, request_data, monkeypatch)
    preview = publication(service, prepare(service, release, skill, batch))
    with service.store.connect() as c:
        c.execute(
            "UPDATE community_evidence SET data=%s WHERE id=%s",
            (Jsonb({"private_receipt": PRIVATE}), preview["id"]),
        )
    assert service.public_evidence()[0]["id"] == preview["id"]
    assert service.leaderboard()["groups"][0]["entries"][0]["eligible"] is True
    assert PRIVATE not in json.dumps(service.public_evidence())


def test_concurrent_publication_is_idempotent_and_consumes_one_publication(
    product, request_data, monkeypatch
):
    service, release, skill = product
    batch = synthetic_batch(product, request_data, monkeypatch)
    preview = prepare(service, release, skill, batch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: publication(service, preview), range(2)))
    assert results[0] == results[1]
    with service.store.connect() as c:
        quota = c.execute("SELECT data FROM community_evidence_limits").fetchone()[
            "data"
        ]
    assert quota["totals"]["publications"] == 1


def test_ties_preserved_and_model_budget_conditions_separate(
    product, request_data, monkeypatch
):
    service, release, skill = product
    batch = synthetic_batch(product, request_data, monkeypatch)
    first = publication(service, prepare(service, release, skill, batch))
    second = publication(service, prepare(service, release, skill, batch))
    group = service.leaderboard()["groups"][0]
    assert set(group["recommendations"]["reliable"]) == {first["id"], second["id"]}
    assert set(group["recommendations"]["cost"]) == {first["id"], second["id"]}
    different = copy.deepcopy(request_data)
    different["model"]["model_id"] = "openai/other-controlled-model"
    other = synthetic_batch(product, different, monkeypatch)
    publication(service, prepare(service, release, skill, other))
    assert len(service.leaderboard()["groups"]) == 2


def test_unknown_cost_and_missing_success_duration_leave_positions_empty(
    product, request_data, monkeypatch
):
    service, release, skill = product
    batch = synthetic_batch(product, request_data, monkeypatch)
    for entry in batch["attempts"]:
        with service.store.locked(entry["run_id"]) as (_, run):
            run["usage"]["cost_usd"] = None
            run["usage"]["unknown_calls"] = 1
            run["test_duration"] = None
    usage = batch["usage"].copy()
    usage.update(cost_usd=None, unknown_calls=len(batch["attempts"]))
    update_batch(service, batch, usage=usage)
    preview = prepare(service, release, skill, batch)
    assert preview["public"]["eligible"] is True
    assert preview["public"]["usage"]["cost_usd"] is None
    assert preview["public"]["usage"]["unknown_calls"] == 15
    publication(service, preview)
    group = service.leaderboard()["groups"][0]
    assert group["recommendations"]["reliable"] == [preview["id"]]
    assert group["recommendations"]["cost"] == group["recommendations"]["speed"] == []


def test_expired_withdrawn_and_release_withdrawn_excluded(
    product, request_data, monkeypatch
):
    from doonex import community_evidence as module
    from doonex.community import WithdrawInput

    service, release, skill = product
    batch = synthetic_batch(product, request_data, monkeypatch)
    preview = publication(service, prepare(service, release, skill, batch))
    original = module.datetime

    class Tomorrow(original):
        @classmethod
        def now(cls, tz=None):
            return original.now(tz) + timedelta(days=2)

    monkeypatch.setattr(module, "datetime", Tomorrow)
    assert service.leaderboard() == {"groups": []} and service.public_evidence() == []
    monkeypatch.setattr(module, "datetime", original)
    service.withdraw(
        preview["id"], WithdrawEvidenceInput(expected_revision=preview["revision"])
    )
    assert service.public_evidence() == []
    publication(service, prepare(service, release, skill, batch))
    service.community.withdraw(
        release["id"],
        WithdrawInput(reason="Synthetic withdrawal"),
    )
    assert service.public_evidence() == [] and service.leaderboard() == {"groups": []}
    assert len(service.list()) == 2


def real_artifact_run(
    product, request_data, tmp_path, state="completed_verified", *, checks=True
):
    service, _, skill = product
    request = copy.deepcopy(request_data)
    request.update(
        skill=skill,
        skill_version_id=skill["version_id"],
        checks=["synthetic-check"] if checks else [],
        prompt=PRIVATE,
    )
    repository = tmp_path / ("repository-" + uuid4().hex)
    repository.mkdir()

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(repository), *args], text=True
        ).strip()

    git("init", "-q")
    git("config", "user.name", "Community evidence fixture")
    git("config", "user.email", "fixture@example.invalid")
    (repository / "code.py").write_text("answer = 1\n")
    git("add", ".")
    git("commit", "-qm", "synthetic source")
    request.update(repo_path=str(repository), base_commit=git("rev-parse", "HEAD"))
    run = service.store.create_run(request)
    directory = run_directory(service.settings, run["id"])
    directory.mkdir(parents=True)
    workspace = Workspace.create(repository, "HEAD", directory / "workspace")
    (workspace.path / "code.py").write_text("answer = 2\n")

    class FixtureRunner:
        image = "synthetic-no-execution"

        def run(self, *args, **kwargs):
            return {
                "exit_code": 0,
                "timed_out": False,
                "cancelled": False,
                "output": PRIVATE,
            }

    report = Evidence.capture(
        workspace, directory, request["checks"], FixtureRunner(), 1, run["id"]
    )
    with service.store.locked(run["id"]) as (_, current):
        current.update(
            state=state,
            first_started_at=now(),
            ended_at=now(),
            evidence={"report_sha256": report["report_sha256"]},
            usage=ledger(),
        )
    return service.store.get(run["id"]), directory


def registration(release, run, distribution_id=None):
    return RegisterUsageInput(
        release_id=release["id"],
        run_id=run["id"],
        distribution_id=distribution_id,
        confirm_register=True,
    )


def test_real_inspection_unique_registration_aggregates_and_preserves_failures(
    product, request_data, tmp_path, monkeypatch
):
    service, release, _ = product
    success, _ = real_artifact_run(product, request_data, tmp_path)
    distribution = service.community.distribute(
        release["id"], DistributionInput(request_id=uuid4())
    )
    body = registration(release, success, distribution["receipt"]["id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.register_usage(body), range(2)))
    assert results[0] == results[1]
    assert results[0]["public"]["successful_acceptance"] is True
    failed, directory = real_artifact_run(
        product, request_data, tmp_path, state="failed"
    )
    (directory / "evidence/diff.patch").write_text("changed")
    failure = service.register_usage(registration(release, failed))
    assert (
        failure["public"]["outcome"] == "failed"
        and not failure["public"]["controller_verified"]
    )
    cancelled, _ = real_artifact_run(product, request_data, tmp_path, state="cancelled")
    service.register_usage(registration(release, cancelled))
    monkeypatch.setattr(
        service.store, "get", lambda *a: pytest.fail("public usage read private Run")
    )
    monkeypatch.setattr(
        service.arena.library,
        "skill_context",
        lambda *a: pytest.fail("public usage read Library"),
    )
    usage = service.public_usage(release["id"])
    assert usage["distribution_requests"] == 1 and usage["self_reported"]["used"] == 0
    assert usage["controller"] == {
        "registrations": 3,
        "verified_uses": 2,
        "successful_acceptances": 1,
        "failed": 1,
        "cancelled": 1,
        "unknown_calls": 0,
    }
    assert usage["commercial"] == {
        "enabled": False,
        "settlement_enabled": False,
        "amount_usd": None,
        "earnings_usd": None,
    }
    assert PRIVATE not in json.dumps(usage) and success["id"] not in json.dumps(usage)
    assert len(service.registrations()) == 3


@pytest.mark.parametrize(
    "change", ["no_skill", "generation", "different_content", "not_started"]
)
def test_generated_unverified_and_non_skill_run_never_count(
    product, request_data, tmp_path, change
):
    service, release, _ = product
    run, _ = real_artifact_run(product, request_data, tmp_path)
    with service.store.locked(run["id"]) as (_, current):
        if change == "no_skill":
            current["request"]["skill"] = None
        elif change == "generation":
            current["request"].update(
                purpose="skill_generation", generation={"id": PRIVATE}
            )
            current["state"] = "completed_unverified"
        elif change == "different_content":
            current["request"]["skill"]["content_digest"] = "0" * 64
        else:
            current["first_started_at"] = None
    with pytest.raises(CommunityError, match="community_usage_requires"):
        service.register_usage(registration(release, run))
    assert service.public_usage(release["id"])["controller"]["registrations"] == 0


def test_stale_or_unbound_artifacts_cannot_claim_success(
    product, request_data, tmp_path
):
    service, release, _ = product
    for change in ("stale", "unbound", "unchecked"):
        run, directory = real_artifact_run(
            product, request_data, tmp_path, checks=change != "unchecked"
        )
        if change == "stale":
            (directory / "workspace/code.py").write_text("different\n")
        elif change == "unbound":
            with service.store.locked(run["id"]) as (_, current):
                current["evidence"] = None
        result = service.register_usage(registration(release, run))
        assert result["public"]["successful_acceptance"] is False
    assert (
        service.public_usage(release["id"])["controller"]["successful_acceptances"] == 0
    )


def test_persistent_capacities_not_reset_by_configuration_or_code_constants(
    product, monkeypatch
):
    from doonex import community_evidence as module

    service, _, _ = product
    with service.store.connect() as c:
        service._consume(c, "preparations")
        current = c.execute(
            "SELECT data FROM community_evidence_limits FOR UPDATE"
        ).fetchone()["data"]
        current["limits"]["preparations"] = {"daily": 1, "total": 1}
        c.execute("UPDATE community_evidence_limits SET data=%s", (Jsonb(current),))
    monkeypatch.setattr(
        module,
        "EVIDENCE_LIMITS",
        {key: {"daily": 9999, "total": 99999} for key in module.EVIDENCE_LIMITS},
    )
    with (
        pytest.raises(CommunityError, match="capacity_exhausted"),
        service.store.connect() as c,
    ):
        service._consume(c, "preparations")
    enabled(service.community)
    with (
        pytest.raises(CommunityError, match="capacity_exhausted"),
        service.store.connect() as c,
    ):
        service._consume(c, "preparations")
    with service.store.connect() as c:
        value = c.execute("SELECT data FROM community_evidence_limits").fetchone()[
            "data"
        ]
    assert value["totals"]["preparations"] == 1


def test_api_admin_auth_and_public_clients_cannot_assert_verified_usage(product):
    service, release, _ = product
    app = FastAPI()

    def auth(authorization: str | None = Header(default=None)):
        if authorization != "Bearer " + service.settings.token:
            raise HTTPException(401)

    install_community_evidence_routes(
        app, service.settings, service.store, [Depends(auth)]
    )
    with TestClient(app) as client:
        assert client.get("/api/community/admin/evidence").status_code == 401
        assert (
            client.post(
                "/api/community/admin/verified-usage",
                json={
                    "release_id": release["id"],
                    "run_id": str(uuid4()),
                    "confirm_register": True,
                },
            ).status_code
            == 401
        )
        response = client.get("/api/community/public/leaderboard")
        assert response.status_code == 200 and response.json() == {"groups": []}
        assert (
            client.post(
                "/api/community/public/verified-usage", json={"verified": True}
            ).status_code
            == 404
        )
        assert (
            PRIVATE
            not in client.get(
                f"/api/community/public/releases/{release['id']}/usage"
            ).text
        )
        assert (
            client.get(
                "/api/community/admin/evidence",
                headers={"Authorization": "Bearer " + service.settings.token},
            ).status_code
            == 200
        )


def test_actual_arena_get_attempt_and_integrity_can_freeze_exact_public_content(
    product, request_data, tmp_path, monkeypatch
):
    """No mocked Arena entry points; fake check runner never executes task code."""
    from doonex.arena import EvaluationInput
    from doonex.worker import get_workspace

    service, release, skill = product
    # Use a real immutable Library task set and real Arena.create, not the
    # controlled classification adapter used by the focused statistics tests.
    seed, _ = real_artifact_run(product, request_data, tmp_path)
    library = service.arena.library
    second_source = tmp_path / "second-candidate"
    second_source.mkdir()
    (second_source / "SKILL.md").write_text("Second controlled Skill.\n")
    second = library.import_skill(second_source, {})
    library.review(second["version_id"], "approved", "Synthetic fixture")
    verifier = tmp_path / "verifier"
    verifier.mkdir()
    (verifier / "check.py").write_text("assert True\n")
    suite = tmp_path / "taskset"
    suite.mkdir()
    (suite / "taskset.json").write_text(
        json.dumps(
            {
                "name": PRIVATE,
                "category": "controlled-tests",
                "split": "heldout",
                "minimum_pass_rate": 0.8,
                "tasks": [
                    {
                        "id": f"task-{index}",
                        "title": PRIVATE,
                        "prompt": PRIVATE,
                        "repo_path": seed["request"]["repo_path"],
                        "base_ref": seed["request"]["base_commit"],
                        "visible_checks": [],
                        "verifier_path": str(verifier),
                        "verifier_command": "python /verifier/check.py",
                        "critical": index == 0,
                    }
                    for index in range(5)
                ],
            }
        )
    )
    taskset = library.import_taskset(suite)
    arena = Arena(service.settings, service.store)
    monkeypatch.setattr(
        arena, "detail", lambda *_: pytest.fail("detail is not a public projection API")
    )
    body = EvaluationInput(
        taskset_id=taskset["id"],
        skill_version_ids=[skill["version_id"], second["version_id"]],
        model_profile_id="legacy",
        repeats=3,
    )
    with service.store.connect() as c:
        batch = arena.create(body, connection=c)

    class FixtureRunner:
        image = "synthetic-no-execution"

        def run(self, *args, **kwargs):
            return {
                "exit_code": 0,
                "timed_out": False,
                "cancelled": False,
                "output": PRIVATE,
            }

    for entry in batch["attempts"]:
        run = service.store.get(entry["run_id"])
        directory = run_directory(service.settings, run["id"])
        directory.mkdir(parents=True)
        Workspace.create(
            seed["request"]["repo_path"],
            seed["request"]["base_commit"],
            directory / "workspace",
        )
        binding = run["request"]["arena"]
        context = library.tasksets.context(
            binding["taskset_id"], binding["report_sha256"], binding["task_id"]
        )
        report = Evidence.capture(
            get_workspace(run, directory),
            directory,
            run["request"]["checks"],
            FixtureRunner(),
            1,
            run["id"],
            verifier_path=context.verifier_path,
            verification_context={
                key: binding[key]
                for key in ("taskset_id", "task_id", "report_sha256", "verifier_digest")
            },
        )
        with service.store.locked(run["id"]) as (_, current):
            current.update(
                state="completed_verified",
                first_started_at=now(),
                ended_at=now(),
                usage=ledger(),
                evidence={"report_sha256": report["report_sha256"]},
                arena_outcome={"recorded_at": now(), "classification": "passed"},
            )
    usage = {key: sum(ledger()[key] for _ in batch["attempts"]) for key in ledger()}
    with arena.locked(batch["id"]) as (_, current):
        current.update(state="complete", usage=usage)
    service.arena = arena
    preview = prepare(service, release, skill, batch)
    assert preview["public"]["eligible"] is True
    assert preview["public"]["counts"]["passed"] == 15
    assert preview["public"]["batch_counts"]["passed"] == 45
    publication(service, preview)
    assert PRIVATE not in json.dumps(service.public_evidence())
    assert service.leaderboard()["groups"][0]["recommendations"]["reliable"] == [
        preview["id"]
    ]

"""Independent community contract review with local synthetic objects only.

These tests do not run a model or upstream code. Archive tests inspect every
member. Concurrency tests hold real PostgreSQL transactions. The Arena helper
uses real immutable tasks and actual Evidence capture/inspection, with a fake
check runner, to probe publication trust anchors independently of adapters.
"""

import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
import test_community_evidence as evidence_tests
from doonex.arena import Arena, EvaluationInput
from doonex.community import (
    COMMUNITY_SCHEMA,
    Community,
    CommunityError,
    DistributionInput,
    PreviewInput,
    ReadInput,
    SettingsInput,
    UsageInput,
    WithdrawInput,
)
from doonex.community_evidence import COMMUNITY_EVIDENCE_SCHEMA
from doonex.discovery import canonical
from doonex.evidence import Evidence
from doonex.store import now
from doonex.worker import get_workspace, run_directory
from doonex.workspace import Workspace
from psycopg.types.json import Jsonb
from test_community import credential, enabled, published, submitted
from test_community_evidence import ledger, prepare, real_artifact_run
from test_discovery import PublicGit

PRIVATE = "PRIVATE-REVIEW-SECRET-PROMPT-HIDDEN-ANSWER-RAW-EVIDENCE-SENTINEL"
product = evidence_tests.product


@pytest.fixture
def reviewed(settings, store):
    with store.connect() as c:
        c.execute(COMMUNITY_SCHEMA)
        c.execute(COMMUNITY_EVIDENCE_SCHEMA)
    source = PublicGit()
    service = Community(settings, store, transport=source.transport)
    enabled(service)
    _, token = credential(service)
    release, _, _ = published(service, source, token)
    return service, source, release, token


def test_review_archive_is_exact_and_contains_no_controller_fields(reviewed):
    service, source, release, _ = reviewed
    distribution = service.distribute(
        release["id"], DistributionInput(request_id=uuid4())
    )
    raw = service.download(distribution["receipt"]["id"])
    assert hashlib.sha256(raw).hexdigest() == release["archive_sha256"]
    with tarfile_open(raw) as archive:
        files = [member for member in archive if member.isfile()]
        assert {member.name for member in files} == set(source.files)
        assert {member.name.rstrip("/") for member in archive if member.isdir()} == set(
            release["directories"]
        )
        assert all(member.isfile() or member.isdir() for member in archive)
        for member in archive:
            assert member.uid == member.gid == member.mtime == 0
            assert member.uname == member.gname == ""
            assert not member.name.startswith("/") and ".." not in member.name.split(
                "/"
            )
            if member.isfile():
                expected = source.files[member.name]
                assert archive.extractfile(member).read() == expected.data
                assert member.mode == expected.mode
            else:
                assert member.mode == 0o700
    assert PRIVATE.encode() not in raw


def tarfile_open(raw):
    import tarfile

    return tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")


@pytest.mark.parametrize("corruption", ["bytes", "mode", "hardlink", "symlink"])
def test_review_tampered_download_does_not_consume_admission(
    reviewed, tmp_path, corruption
):
    service, _, release, _ = reviewed
    distribution = service.distribute(
        release["id"], DistributionInput(request_id=uuid4())
    )
    with service.store.connect() as c:
        artifact = c.execute(
            "SELECT artifact FROM community_releases WHERE id=%s", (release["id"],)
        ).fetchone()["artifact"]
    archive = service.root / artifact["preview_sha256"] / "candidate.tar.gz"
    if corruption == "bytes":
        archive.write_bytes(archive.read_bytes() + b"changed")
    elif corruption == "mode":
        archive.chmod(0o644)
    elif corruption == "hardlink":
        (tmp_path / "duplicate.tar.gz").hardlink_to(archive)
    else:
        target = tmp_path / "target.tar.gz"
        target.write_bytes(archive.read_bytes())
        archive.unlink()
        archive.symlink_to(target)
    with pytest.raises(ValueError):
        service.download(distribution["receipt"]["id"])
    with service.store.connect() as c:
        saved = c.execute(
            "SELECT data FROM community_distributions WHERE id=%s",
            (distribution["receipt"]["id"],),
        ).fetchone()["data"]
    assert saved["downloads"] == 0 and saved["download_admissions"] == []
    assert service.settings_get()["totals"]["downloads"] == 0


def test_review_nested_private_sentinels_never_enter_public_projection(reviewed):
    service, _, release, _ = reviewed
    with service.store.connect() as c:
        original = c.execute(
            "SELECT data,artifact FROM community_releases WHERE id=%s", (release["id"],)
        ).fetchone()
        original["data"]["review"].update(
            note=PRIVATE, raw={"prompt": PRIVATE, "run_id": PRIVATE}
        )
        original["data"]["private"] = {
            "model": PRIVATE,
            "project": PRIVATE,
            "raw_evidence": PRIVATE,
        }
        original["artifact"].update(
            host_path=PRIVATE, raw_credentials={"value": PRIVATE}
        )
        c.execute(
            "UPDATE community_releases SET data=%s,artifact=%s WHERE id=%s",
            (Jsonb(original["data"]), Jsonb(original["artifact"]), release["id"]),
        )
    assert PRIVATE not in json.dumps(service.release(release["id"]))
    assert PRIVATE not in json.dumps(service.releases())
    assert PRIVATE not in json.dumps(service.public_site())
    distribution = service.distribute(
        release["id"], DistributionInput(request_id=uuid4())
    )
    assert PRIVATE not in json.dumps(distribution)
    assert PRIVATE.encode() not in service.download(distribution["receipt"]["id"])


def test_review_usage_token_is_scope_limited_and_revocation_preserves_history(reviewed):
    service, source, release, submission_token = reviewed
    distribution = service.distribute(
        release["id"], DistributionInput(request_id=uuid4())
    )
    usage_token = "Bearer " + distribution["usage_token"]
    event = UsageInput(event_id=uuid4(), outcome="used")
    recorded = service.self_report(usage_token, event)
    with pytest.raises(CommunityError, match="community_token_invalid"):
        submitted(service, source, usage_token)
    with pytest.raises(CommunityError, match="community_token_invalid"):
        service.self_report(submission_token, event)
    with service.store.connect() as c:
        value = c.execute(
            "SELECT data FROM community_distributions WHERE id=%s",
            (distribution["receipt"]["id"],),
        ).fetchone()["data"]
    service.revoke(value["usage_token_id"])
    with pytest.raises(CommunityError, match="community_token_inactive"):
        service.self_report(usage_token, event)
    with service.store.connect() as c:
        rows = c.execute("SELECT data FROM community_usage_reports").fetchall()
    assert len(rows) == 1 and rows[0]["data"] == recorded


def test_review_disable_blocks_new_submission_distribution_download_and_reports(
    reviewed,
):
    service, source, release, submission_token = reviewed
    distribution = service.distribute(
        release["id"], DistributionInput(request_id=uuid4())
    )
    current = service.settings_get()
    service.configure(
        SettingsInput(
            enabled=False, title=current["title"], expected_revision=current["revision"]
        )
    )
    for action in (
        lambda: submitted(service, source, submission_token),
        lambda: service.distribute(
            release["id"], DistributionInput(request_id=uuid4())
        ),
        lambda: service.download(distribution["receipt"]["id"]),
        lambda: service.self_report(
            "Bearer " + distribution["usage_token"],
            UsageInput(event_id=uuid4(), outcome="used"),
        ),
        service.public_site,
        service.releases,
    ):
        with pytest.raises(CommunityError, match="community_disabled"):
            action()


def test_review_disabled_tokens_can_read_own_history_and_existing_event(reviewed):
    service, _, release, submission_token = reviewed
    own = service.submissions(submission_token)
    distribution = service.distribute(
        release["id"], DistributionInput(request_id=uuid4())
    )
    usage_token = "Bearer " + distribution["usage_token"]
    event = UsageInput(event_id=uuid4(), outcome="used")
    recorded = service.self_report(usage_token, event)
    current = service.settings_get()
    service.configure(
        SettingsInput(
            enabled=False, title=current["title"], expected_revision=current["revision"]
        )
    )
    assert service.submissions(submission_token) == own
    assert service.get_submission(own[0]["id"], submission_token) == own[0]
    assert service.self_report(usage_token, event) == recorded


def test_review_withdrawal_serializes_after_admitted_download(reviewed, monkeypatch):
    from doonex import community as module

    service, _, release, _ = reviewed
    distribution = service.distribute(
        release["id"], DistributionInput(request_id=uuid4())
    )
    reading, continue_read, withdrawing = Event(), Event(), Event()
    real_read = module.private_read

    def held_read(path, *args, **kwargs):
        reading.set()
        assert continue_read.wait(10)
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(module, "private_read", held_read)

    def withdraw():
        withdrawing.set()
        return service.withdraw(
            release["id"], WithdrawInput(reason="Synthetic concurrent withdrawal")
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        download = pool.submit(service.download, distribution["receipt"]["id"])
        assert reading.wait(10)
        withdrawal = pool.submit(withdraw)
        assert withdrawing.wait(10)
        assert not withdrawal.done()
        continue_read.set()
        assert download.result(timeout=10)
        assert withdrawal.result(timeout=10)["state"] == "withdrawn"
    with pytest.raises(CommunityError, match="community_release_not_found"):
        service.download(distribution["receipt"]["id"])
    assert (
        len(
            service.distribution_binding(distribution["receipt"]["id"])[
                "download_admissions"
            ]
        )
        == 1
    )


def test_review_unknown_source_preview_cannot_replay_after_restart(
    reviewed, monkeypatch
):
    service, source, _, token = reviewed
    own = submitted(service, source, token)
    value = service.get_submission(own["id"])
    read = service.read(
        value["id"], ReadInput(expected_revision=value["revision"], request_id=uuid4())
    )
    candidate_id = read["discovery"]["candidates"][0]["id"]

    def failure(*args):
        raise OSError(PRIVATE)

    monkeypatch.setattr(service.discovery, "preview", failure)
    with pytest.raises(CommunityError, match="community_source_operation_failed"):
        service.preview(
            read["id"],
            PreviewInput(
                expected_revision=read["revision"],
                request_id=uuid4(),
                candidate_id=candidate_id,
            ),
        )
    unknown = service.get_submission(read["id"])
    assert (
        unknown["state"] == "source_unknown"
        and unknown["operation"]["kind"] == "previews"
    )
    assert PRIVATE not in json.dumps(service.get_submission(read["id"], token))
    restarted = Community(service.settings, service.store, transport=source.transport)
    count = len(source.calls)
    with pytest.raises(CommunityError, match="community_operation_not_replayable"):
        restarted.preview(
            read["id"],
            PreviewInput(
                expected_revision=unknown["revision"],
                request_id=uuid4(),
                candidate_id=candidate_id,
            ),
        )
    assert len(source.calls) == count
    assert (
        restarted.settings_get()["totals"]["previews"] == 2
    )  # one initial release + unknown attempt


def _actual_batch(product, request_data, tmp_path):
    service, _, skill = product
    seed, _ = real_artifact_run(product, request_data, tmp_path)
    library = service.arena.library
    source = tmp_path / "second-review-candidate"
    source.mkdir()
    (source / "SKILL.md").write_text("Second synthetic review candidate.\n")
    second = library.import_skill(source, {})
    library.review(second["version_id"], "approved", "Synthetic fixture")
    verifier = tmp_path / "review-verifier"
    verifier.mkdir()
    (verifier / "check.py").write_text("assert True\n")
    suite = tmp_path / "review-taskset"
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
    with service.store.connect() as c:
        batch = arena.create(
            EvaluationInput(
                taskset_id=taskset["id"],
                skill_version_ids=[skill["version_id"], second["version_id"]],
                model_profile_id="legacy",
                repeats=3,
            ),
            connection=c,
        )

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
    with arena.locked(batch["id"]) as (_, current):
        current.update(
            state="complete",
            usage={
                key: sum(ledger()[key] for _ in batch["attempts"]) for key in ledger()
            },
        )
    service.arena = arena
    return batch


def test_review_public_arena_requires_database_digest_and_actual_report_run_identity(
    product, request_data, tmp_path
):
    service, release, skill = product
    batch = _actual_batch(product, request_data, tmp_path)
    selected = next(
        entry for entry in batch["attempts"] if entry["arm_id"] == skill["version_id"]
    )
    run_id = selected["run_id"]
    original = service.store.get(run_id)["evidence"]
    assert prepare(service, release, skill, batch)["public"]["eligible"] is True
    observations = {}
    with service.store.locked(run_id) as (_, run):
        run["evidence"] = None
    try:
        observations["missing_database_report_digest"] = prepare(
            service, release, skill, batch
        )["public"]["eligible"]
    except CommunityError:
        observations["missing_database_report_digest"] = False
    with service.store.locked(run_id) as (_, run):
        run["evidence"] = original
    root = run_directory(service.settings, run_id) / "evidence"
    report = json.loads((root / "report.json").read_bytes())
    report["run_id"] = str(uuid4())
    encoded = canonical(report)
    changed = hashlib.sha256(encoded).hexdigest()
    (root / "report.json").write_bytes(encoded)
    (root / "report.sha256").write_text(changed + "\n")
    with service.store.locked(run_id) as (_, run):
        run["evidence"] = {"report_sha256": changed}
    try:
        observations["foreign_report_run_id"] = prepare(service, release, skill, batch)[
            "public"
        ]["eligible"]
    except CommunityError:
        observations["foreign_report_run_id"] = False
    assert observations == {
        "missing_database_report_digest": False,
        "foreign_report_run_id": False,
    }, observations

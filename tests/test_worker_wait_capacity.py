"""Published-capacity paths through actual Worker.run, PostgreSQL and Docker.

All provider responses and user queue actions are explicit local fixtures. The
scheduler, durable controller, SDK, ledgers, Git export and verification run
normally. Sample elapsed times are observations, never a release waiting SLA.
"""

import hashlib
import json
import os
import re
import subprocess
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

import controller_test_support
import pytest
from doonex import sdk
from doonex import workspace as workspace_module
from doonex.budget import Ledger
from doonex.cleanup import sandbox_context
from doonex.sandbox import TASK_IMAGE, Sandbox
from doonex.scheduling import Scheduler
from doonex.worker import MAX_YIELDED_RUNS, Worker, run_directory
from psycopg import sql
from test_api import repository
from test_scheduling import action_rows, claimed
from test_scheduling import taskset_source as taskset_fixture
from test_scheduling import tuning as tuning_fixture
from test_worker import response
from test_worker_wait_matrix import (
    test_actual_worker_wait_timeline as chat_timeline_fixture,
)

controller_registry = controller_test_support.controller_registry
taskset_source = taskset_fixture
tuning = tuning_fixture
pytestmark = [pytest.mark.postgres, pytest.mark.docker]


@pytest.fixture(autouse=True)
def preserve_capacity_durable_rows(store):
    """Observe failures before the standard disposable Store fixture closes."""
    try:
        yield
    finally:
        with store.connect() as connection:
            rows = {
                table: connection.execute(
                    sql.SQL("SELECT * FROM {} ORDER BY 1").format(sql.Identifier(table))
                ).fetchall()
                for table in (
                    "runs",
                    "events",
                    "actions",
                    "controller_operations",
                    "arena_evaluations",
                    "background_authorizations",
                    "background_allocations",
                    "background_days",
                )
            }
        print("worker_capacity_durable_rows=" + json.dumps(rows, default=str))


def background_accounting(store):
    """Compare actual clocks, usage and retained shared reservations verbatim."""
    with store.connect() as connection:
        children = connection.execute(
            "SELECT data FROM runs WHERE "
            "jsonb_typeof(data->'request'->'background')='object' "
            "ORDER BY id"
        ).fetchall()
        actions = connection.execute(
            "SELECT actions.* FROM actions JOIN runs ON runs.id=actions.run_id "
            "WHERE jsonb_typeof(runs.data->'request'->'background')='object' "
            "ORDER BY actions.id"
        ).fetchall()
        result = {
            "children": [
                {
                    key: deepcopy(row["data"].get(key))
                    for key in (
                        "id",
                        "request",
                        "owner",
                        "first_started_at",
                        "usage",
                        "scheduling",
                    )
                }
                for row in children
            ],
            "actions": actions,
        }
        for table in (
            "arena_evaluations",
            "background_authorizations",
            "background_allocations",
            "background_days",
        ):
            values = [
                row["data"]
                for row in connection.execute(
                    sql.SQL("SELECT data FROM {} ORDER BY 1").format(
                        sql.Identifier(table)
                    )
                )
            ]
            if table == "arena_evaluations":
                # sync legitimately refreshes this timestamp and, for an
                # observed tail residual, marks its parent interrupted.
                values = [
                    {
                        key: value
                        for key, value in row.items()
                        if key not in {"updated_at", "state", "reason"}
                    }
                    for row in values
                ]
            result[table] = values
        return result


def register_owned(*, run_ids=(), container_ids=()):
    """Give an external watchdog exact fixture identities, without credentials."""
    raw = os.environ.get("DOONEX_TEST_OWNERSHIP_FILE")
    if not raw:
        return
    path = Path(raw)
    record = json.loads(path.read_text())
    for key, values in (("run_ids", run_ids), ("container_ids", container_ids)):
        record[key] = sorted(set(record.get(key, ())) | set(values))
    temporary = path.with_suffix(".pending")
    temporary.write_text(json.dumps(record, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def actual_yielded(store):
    with store.connect() as connection:
        return [
            row["data"]
            for row in connection.execute(
                "SELECT data FROM runs WHERE state='yielded' "
                "ORDER BY data->>'created_at' DESC,id DESC"
            )
        ]


def checkpoint_binding(run):
    return {
        key: deepcopy(run.get(key))
        for key in ("id", "state", "owner", "first_started_at", "usage", "scheduling")
    }


@dataclass
class RealCheckpoints:
    settings: object
    batch: dict
    foreground_request: dict
    foreground: dict
    yielded: list[dict]
    observations: list[dict]
    selected: list[str]


def build_real_checkpoints(tuning, store, request_data, tmp_path, monkeypatch, count):
    """Generate distinct checkpoints without selecting or executing a fake Run.

    Each round is a new actual Worker.run(once=True). A real ordinary enqueue in
    the provider causes a yield. On the next round, existing yielded Runs must
    all pass the real container probe and decline resume for that queued user
    task. The fixture then simulates that user's cancellation after the final
    probe; the original queue naturally selects the next background sibling.
    """
    assert count in {1, MAX_YIELDED_RUNS, MAX_YIELDED_RUNS + 1}
    settings, background, body, source_repo = tuning
    candidate = tmp_path / "third-approved-skill"
    candidate.mkdir()
    (candidate / "SKILL.md").write_text("Inspect and verify. Third synthetic skill.\n")
    skill = background.library.import_skill(candidate, {})
    background.library.review(skill["version_id"], "approved", "Synthetic capacity")
    body = deepcopy(body)
    body["skill_version_ids"].append(skill["version_id"])
    settings, _, _, batch = claimed((settings, background, body, source_repo), store)
    assert len(batch["attempts"]) == 20
    register_owned(run_ids=[item["run_id"] for item in batch["attempts"]])
    foreground_path = tmp_path / "capacity-foreground-source"
    foreground_path.mkdir()
    repo, git = repository(foreground_path)
    foreground_request = deepcopy(request_data)
    foreground_request.update(
        repo_path=str(repo),
        base_commit=git("rev-parse", "HEAD"),
        checks=["python -c 'assert True'"],
    )
    foreground_request["model"].update(model_id="gpt-4.1-mini")
    observations, selected, queued = [], [], []
    stage = {"cancel_after_census": False, "remaining": set(), "active": None}
    original_execute = Worker.execute
    original_resume = Scheduler.resume_yielded
    original_containers = Sandbox.containers

    def observe(kind, **details):
        observations.append({"kind": kind, "monotonic": time.monotonic(), **details})

    def execute(worker, run_id):
        selected.append(run_id)
        stage["active"] = run_id
        observe("actual_execute_enter", run_id=run_id)
        try:
            return original_execute(worker, run_id)
        finally:
            observe("actual_execute_returned", run_id=run_id)
            stage["active"] = None

    def containers(sandbox, run_id, *args, **kwargs):
        result = original_containers(sandbox, run_id, *args, **kwargs)
        observe("actual_container_probe_returned", run_id=run_id, containers=result)
        return result

    def resume(scheduler, run, workspace, *args, **kwargs):
        value = original_resume(scheduler, run, workspace, *args, **kwargs)
        observe("actual_resume_yielded_returned", run_id=run["id"], resumed=value)
        if stage["cancel_after_census"]:
            assert not value
            stage["remaining"].remove(run["id"])
            if not stage["remaining"]:
                assert len(queued) == 1
                current = store.get(queued.pop())
                assert current["state"] == "queued"
                store.transition(
                    current["id"], "cancelled", "Synthetic user cancelled after probes"
                )
                observe("synthetic_user_cancel_committed", run_id=current["id"])
        return value

    def transport(*args, **kwargs):
        identifier = stage["active"]
        assert identifier is not None
        run = store.get(identifier)
        observe("synthetic_provider_enter", run_id=identifier)
        if not run["request"].get("background"):
            return response("finish", {"message": "Synthetic capacity foreground"}, 0)
        assert not queued
        foreground = store.create_run(foreground_request)
        register_owned(run_ids=[foreground["id"]])
        queued.append(foreground["id"])
        observe("synthetic_user_enqueue_commit_returned", run_id=foreground["id"])
        answer = response("doonex_shell", {"command": "touch forbidden-replay"}, 0)
        answer.choices[0].message.tool_calls.extend(
            response("finish", {"message": "Synthetic yielded capacity"}, 1)
            .choices[0]
            .message.tool_calls
        )
        return answer

    monkeypatch.setattr(Worker, "execute", execute)
    monkeypatch.setattr(Scheduler, "resume_yielded", resume)
    monkeypatch.setattr(Sandbox, "containers", containers)
    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", transport)
    for index in range(count):
        previous = actual_yielded(store)
        assert len(previous) == index
        stage["remaining"] = {run["id"] for run in previous}
        stage["cancel_after_census"] = bool(previous)
        before_selected = len(selected)
        Worker(settings).run(once=True)
        assert len(selected) == before_selected + 1
        current = store.get(selected[-1])
        assert current["request"].get("background")
        assert current["state"] == "yielded", current["reason"]
        assert not stage["remaining"]
        assert len(actual_yielded(store)) == index + 1
        assert current["usage"]["calls"] == 1
        assert current["usage"]["unknown_calls"] == 0
        assert current["usage"]["reserved_tokens"] == 0
        assert len(current["scheduling"]["receipt"]["pending_actions"]) == 2
        assert [row["kind"] for row in action_rows(store, current["id"])] == ["model"]
        assert not store.pending(current["id"])
        assert not (
            run_directory(settings, current["id"]) / "workspace/forbidden-replay"
        ).exists()
    stage["cancel_after_census"] = False
    yielded = actual_yielded(store)
    assert len({run["id"] for run in yielded}) == count
    assert len({run["scheduling"]["receipt"]["session_id"] for run in yielded}) == count
    assert (
        len({run["scheduling"]["receipt"]["leaf_event_id"] for run in yielded}) == count
    )
    return RealCheckpoints(
        settings,
        batch,
        foreground_request,
        store.get(queued[0]),
        yielded,
        observations,
        selected,
    )


@pytest.mark.parametrize("scenario", ["full", "tail_container", "overflow"])
def test_actual_worker_complete_yielded_census(
    tuning, store, request_data, tmp_path, monkeypatch, scenario
):
    count = MAX_YIELDED_RUNS + (scenario == "overflow")
    checkpoints = build_real_checkpoints(
        tuning, store, request_data, tmp_path, monkeypatch, count
    )
    before = [checkpoint_binding(run) for run in checkpoints.yielded]
    accounting_before = background_accounting(store)
    with store.connect() as connection:
        parent_state_before = connection.execute(
            "SELECT state FROM arena_evaluations WHERE id=%s",
            (checkpoints.batch["id"],),
        ).fetchone()["state"]
    offset = len(checkpoints.observations)
    selected_offset = len(checkpoints.selected)
    container_id = None
    fixture_setup = None
    try:
        if scenario == "tail_container":
            tail_run = checkpoints.yielded[-1]
            tail = tail_run["id"]
            workspace = run_directory(checkpoints.settings, tail) / "workspace"
            # Execute a harmless, real sandbox operation to bind its immutable
            # name to this actual Run and owner in the durable registry. After
            # its confirmed cleanup, the fixture explicitly simulates an
            # external residual using that same name and exact workspace mount.
            setup = Sandbox().run(
                workspace, "true", 10, tail, **sandbox_context(tail_run)
            )
            assert setup["exit_code"] == 0
            assert not setup["cleanup_error"] and not setup["cleanup_unknown"]
            operation_id = setup["cleanup_evidence"]["controller_operation_id"]
            with store.connect() as connection:
                operation = connection.execute(
                    "SELECT data FROM controller_operations WHERE id=%s",
                    (operation_id,),
                ).fetchone()["data"]
            fixture_name = operation["scope"]["container_name"]
            assert operation["run_id"] == tail
            assert operation["scope"]["run_label"] == "doonex.run_id=" + tail
            assert operation["terminal_evidence"]["docker"]["container_removed"]
            fixture_setup = {
                "operation_id": operation_id,
                "container_name": fixture_name,
                "workspace_mount": str(workspace),
                "synthetic_external_residual_after_confirmed_cleanup": True,
            }
            created = subprocess.run(
                [
                    "docker",
                    "create",
                    "--name",
                    fixture_name,
                    "--label",
                    "doonex.run_id=" + tail,
                    "--label",
                    "doonex.capacity_fixture=" + fixture_name,
                    "--network",
                    "none",
                    "--mount",
                    f"type=bind,src={workspace},dst=/workspace",
                    TASK_IMAGE,
                    "true",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            container_id = created.stdout.strip()
            assert re.fullmatch(r"[a-f0-9]{64}", container_id)
            register_owned(container_ids=[container_id])
            labels = subprocess.run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{json .Config.Labels}}",
                    container_id,
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            assert json.loads(labels.stdout)["doonex.run_id"] == tail
        worker = Worker(checkpoints.settings)
        started = time.monotonic()
        if scenario == "overflow":
            with pytest.raises(ValueError, match="调度核查容量超限"):
                worker.run(once=True)
        elif scenario == "tail_container":
            with pytest.raises(RuntimeError, match="后台检查点存在残留或未知容器"):
                worker.run(once=True)
        else:
            worker.run(once=True)
        finished = time.monotonic()
        events = checkpoints.observations[offset:]
        probes = [
            event
            for event in events
            if event["kind"] == "actual_container_probe_returned"
        ]
        resumed = [
            event
            for event in events
            if event["kind"] == "actual_resume_yielded_returned"
        ]
        selected = checkpoints.selected[selected_offset:]
        if scenario == "overflow":
            assert not probes and not resumed and not selected
        else:
            assert [event["run_id"] for event in resumed] == [
                run["id"] for run in checkpoints.yielded
            ]
            assert all(not event["resumed"] for event in resumed)
            assert [event["run_id"] for event in probes[:count]] == [
                run["id"] for run in checkpoints.yielded
            ]
            if scenario == "tail_container":
                assert not selected
                assert container_id in probes[count - 1]["containers"]
            else:
                assert selected == [checkpoints.foreground["id"]]
                final = store.get(checkpoints.foreground["id"])
                assert final["state"] == "completed_verified", final["reason"]
        after = [
            checkpoint_binding(store.get(run["id"])) for run in checkpoints.yielded
        ]
        if scenario == "tail_container":
            assert after[-1]["state"] == "interrupted"
            preserved_tail = dict(after[-1], state=before[-1]["state"])
            assert before == [*after[:-1], preserved_tail]
        else:
            assert before == after
        assert accounting_before == background_accounting(store)
        with store.connect() as connection:
            parent_state_after = connection.execute(
                "SELECT state FROM arena_evaluations WHERE id=%s",
                (checkpoints.batch["id"],),
            ).fetchone()["state"]
        assert parent_state_after == (
            "interrupted" if scenario == "tail_container" else parent_state_before
        )
        print(
            "worker_yielded_capacity="
            + json.dumps(
                {
                    "scenario": scenario,
                    "yielded_count": count,
                    "published_yielded_capacity": MAX_YIELDED_RUNS,
                    "worker_started_monotonic": started,
                    "worker_returned_monotonic": finished,
                    "events": events,
                    "selected": selected,
                    "checkpoint_bindings": before,
                    "fixture_setup": fixture_setup,
                    "background_accounting_preserved": True,
                    "real_model_calls": 0,
                    "normal_wait_sla": False,
                },
                sort_keys=True,
            )
        )
    finally:
        if container_id is not None:
            removed = subprocess.run(
                ["docker", "rm", "-f", container_id],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            remaining = subprocess.run(
                [
                    "docker",
                    "ps",
                    "-a",
                    "--no-trunc",
                    "--filter",
                    "id=" + container_id,
                    "--format",
                    "{{.ID}}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            assert removed.returncode == remaining.returncode == 0
            assert not remaining.stdout.strip() and not remaining.stderr.strip()


def git_command(repo, *arguments, data=None):
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        input=data,
        capture_output=True,
        timeout=30,
        check=True,
    )
    return result.stdout


def synthetic_git_capacity(repo, shape):
    """Construct real Git objects; generated entries need no host checkout."""
    small_blob = (
        git_command(repo, "hash-object", "-w", "--stdin", data=b"x\n").strip().decode()
    )
    hello_blob = git_command(repo, "rev-parse", "HEAD:hello.py").strip().decode()

    def tree(records):
        payload = b"".join(
            f"{mode} {kind} {identity}\t{name}\0".encode()
            for name, mode, kind, identity in sorted(records)
        )
        return git_command(repo, "mktree", "-z", data=payload).strip().decode()

    records = [("hello.py", "100644", "blob", hello_blob)]
    if shape in {"raw_entries_near", "raw_entries_over"}:
        count = (
            workspace_module.MAX_GIT_TREE_ENTRIES
            + (1 if shape.endswith("over") else -1)
            - 1
        )
        generated = tree(
            [
                (f"e{index:05d}.pyc", "100644", "blob", small_blob)
                for index in range(count)
            ]
        )
        records.append(("__pycache__", "040000", "tree", generated))
    elif shape in {"raw_bytes_near", "raw_bytes_over"}:
        # Fixed-width names let actual ls-tree record size determine the count.
        prefix = [character * 240 for character in "pqrs"] + ["__pycache__"]
        sample = tree([("e00000.pyc", "100644", "blob", small_blob)])
        sample_records = [(prefix[-1], "040000", "tree", sample)]
        for part in reversed(prefix[:-1]):
            sample_records = [(part, "040000", "tree", tree(sample_records))]
        sample_root = tree(records + sample_records)
        sample_listing = git_command(repo, "ls-tree", "-r", "-z", "-l", sample_root)
        record_bytes = next(
            len(record) + 1
            for record in sample_listing.split(b"\0")
            if b"__pycache__" in record
        )
        fixed_bytes = len(sample_listing) - record_bytes
        count = (workspace_module.MAX_GIT_TREE_BYTES - fixed_bytes) // record_bytes
        if shape.endswith("over"):
            count += 1
        generated = tree(
            [
                (f"e{index:05d}.pyc", "100644", "blob", small_blob)
                for index in range(count)
            ]
        )
        nested = [(prefix[-1], "040000", "tree", generated)]
        for part in reversed(prefix[:-1]):
            nested = [(part, "040000", "tree", tree(nested))]
        records.extend(nested)
    elif shape in {"retained_entries_near", "retained_entries_over"}:
        count = workspace_module.MAX_FILES + (1 if shape.endswith("over") else -1) - 1
        records.extend(
            (f"retained-{index:05d}.txt", "100644", "blob", small_blob)
            for index in range(count)
        )
    elif shape in {"retained_bytes_near", "retained_bytes_over", "single_file_over"}:
        records = []
        total = workspace_module.MAX_TOTAL_BYTES + (
            1 if shape.endswith("over") else -1024
        )
        if shape == "single_file_over":
            sizes = [workspace_module.MAX_FILE_BYTES + 1]
        else:
            sizes = [workspace_module.MAX_FILE_BYTES] * (
                total // workspace_module.MAX_FILE_BYTES
            )
            if total % workspace_module.MAX_FILE_BYTES:
                sizes.append(total % workspace_module.MAX_FILE_BYTES)
        for index, size in enumerate(sizes):
            blob = (
                git_command(repo, "hash-object", "-w", "--stdin", data=b"x" * size)
                .strip()
                .decode()
            )
            records.append((f"retained-{index:02d}.dat", "100644", "blob", blob))
    else:
        raise AssertionError(shape)
    root = tree(records)
    previous = git_command(repo, "rev-parse", "HEAD").strip().decode()
    commit = (
        git_command(
            repo,
            "commit-tree",
            root,
            "-p",
            previous,
            data=b"Synthetic capacity fixture\n",
        )
        .strip()
        .decode()
    )
    git_command(repo, "update-ref", "HEAD", commit)
    listing = git_command(repo, "ls-tree", "-r", "-z", "-l", commit)
    raw_records = [record for record in listing.split(b"\0") if record]
    retained = [record for record in raw_records if b"__pycache__" not in record]
    return {
        "shape": shape,
        "commit": commit,
        "tree": root,
        "raw_entries": len(raw_records),
        "raw_tree_bytes": len(listing),
        "retained_entries": len(retained),
        "retained_bytes": sum(
            int(record.split(b"\t", 1)[0].split()[-1]) for record in retained
        ),
        "raw_listing_sha256": hashlib.sha256(listing).hexdigest(),
        "status_sha256": hashlib.sha256(
            git_command(repo, "status", "--porcelain=v1", "-z")
        ).hexdigest(),
        "index_sha256": hashlib.sha256((repo / ".git/index").read_bytes()).hexdigest(),
        "generated_objects_materialized_in_source": False,
    }


@pytest.mark.parametrize(
    "shape",
    [
        "raw_entries_near",
        "raw_bytes_near",
        "retained_entries_near",
        "retained_bytes_near",
    ],
)
def test_actual_worker_near_git_export_capacity(
    tuning, taskset_source, store, request_data, tmp_path, monkeypatch, shape
):
    import test_worker_wait_matrix as matrix

    actual_repository = matrix.repository
    observed = []

    def capacity_repository(path):
        repo, git = actual_repository(path)
        observed.append((repo, synthetic_git_capacity(repo, shape)))
        return repo, git

    monkeypatch.setattr(matrix, "repository", capacity_repository)
    chat_timeline_fixture(
        tuning,
        taskset_source,
        store,
        request_data,
        tmp_path,
        monkeypatch,
        "model",
        "ordinary",
    )
    assert len(observed) == 1
    repo, dimensions = observed[0]
    assert (
        dimensions["status_sha256"]
        == hashlib.sha256(
            git_command(repo, "status", "--porcelain=v1", "-z")
        ).hexdigest()
    )
    assert (
        dimensions["index_sha256"]
        == hashlib.sha256((repo / ".git/index").read_bytes()).hexdigest()
    )
    assert dimensions["raw_entries"] <= workspace_module.MAX_GIT_TREE_ENTRIES
    assert dimensions["raw_tree_bytes"] <= workspace_module.MAX_GIT_TREE_BYTES
    assert dimensions["retained_entries"] <= workspace_module.MAX_FILES
    assert dimensions["retained_bytes"] <= workspace_module.MAX_TOTAL_BYTES
    if shape == "raw_entries_near":
        assert dimensions["raw_entries"] == workspace_module.MAX_GIT_TREE_ENTRIES - 1
    elif shape == "raw_bytes_near":
        assert (
            0
            < workspace_module.MAX_GIT_TREE_BYTES - dimensions["raw_tree_bytes"]
            < 1200
        )
    elif shape == "retained_entries_near":
        assert dimensions["retained_entries"] == workspace_module.MAX_FILES - 1
    else:
        assert dimensions["retained_bytes"] == workspace_module.MAX_TOTAL_BYTES - 1024
    print("worker_git_capacity=" + json.dumps(dimensions, sort_keys=True))


@pytest.mark.parametrize(
    "shape",
    [
        "raw_entries_over",
        "raw_bytes_over",
        "retained_entries_over",
        "retained_bytes_over",
        "single_file_over",
    ],
)
def test_actual_worker_rejects_over_git_capacity_before_model(
    tuning, store, request_data, tmp_path, monkeypatch, shape
):
    settings, _, _, _ = tuning
    source = tmp_path / "over-capacity-source"
    source.mkdir()
    repo, git = repository(source)
    dimensions = synthetic_git_capacity(repo, shape)
    body = deepcopy(request_data)
    body.update(
        repo_path=str(repo),
        base_commit=git("rev-parse", "HEAD"),
        checks=["python -c 'assert True'"],
    )
    body["model"].update(model_id="gpt-4.1-mini")
    created = store.create_run(body)
    register_owned(run_ids=[created["id"]])

    def forbidden(*args, **kwargs):
        pytest.fail("An over-capacity Git fixture reached a model transport")

    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", forbidden)
    started = time.monotonic()
    Worker(settings).run(once=True)
    finished = time.monotonic()
    final = store.get(created["id"])
    assert final["state"] == "failed", final["reason"]
    assert not action_rows(store, created["id"])
    assert final["usage"]["calls"] == final["usage"]["unknown_calls"] == 0
    assert final["usage"]["reserved_tokens"] == 0
    assert final["first_started_at"] is not None
    assert not store.pending(created["id"])
    assert not final.get("scheduling")
    assert not final["recovery"]["errors"]
    assert (
        dimensions["status_sha256"]
        == hashlib.sha256(
            git_command(repo, "status", "--porcelain=v1", "-z")
        ).hexdigest()
    )
    assert (
        dimensions["index_sha256"]
        == hashlib.sha256((repo / ".git/index").read_bytes()).hexdigest()
    )
    if shape == "raw_entries_over":
        assert dimensions["raw_entries"] == workspace_module.MAX_GIT_TREE_ENTRIES + 1
    elif shape == "raw_bytes_over":
        assert dimensions["raw_tree_bytes"] > workspace_module.MAX_GIT_TREE_BYTES
        assert dimensions["raw_entries"] <= workspace_module.MAX_GIT_TREE_ENTRIES
    elif shape == "retained_entries_over":
        assert dimensions["retained_entries"] == workspace_module.MAX_FILES + 1
    elif shape == "retained_bytes_over":
        assert dimensions["retained_bytes"] == workspace_module.MAX_TOTAL_BYTES + 1
    else:
        assert dimensions["retained_bytes"] == workspace_module.MAX_FILE_BYTES + 1
    print(
        "worker_git_capacity_refused="
        + json.dumps(
            {
                **dimensions,
                "worker_started_monotonic": started,
                "worker_returned_monotonic": finished,
                "run_id": created["id"],
                "state": final["state"],
                "reason": final["reason"],
                "usage": final["usage"],
                "recovery": final["recovery"],
                "real_model_calls": 0,
                "normal_wait_sla": False,
            },
            sort_keys=True,
        )
    )


def test_actual_worker_reports_target_with_preceding_foreground_separately(
    tuning, store, request_data, tmp_path, monkeypatch
):
    checkpoints = build_real_checkpoints(
        tuning, store, request_data, tmp_path, monkeypatch, 1
    )
    first_id = checkpoints.foreground["id"]
    before = time.monotonic()
    target = store.create_run(checkpoints.foreground_request)
    after = time.monotonic()
    register_owned(run_ids=[target["id"]])
    admissions, settlements, active_model = [], [], []
    original_reserve = Ledger.reserve
    original_settle = Ledger.settle
    original_transport = sdk.GuardedLLM._transport_call
    predecessor_hold_seconds = 0.75

    def reserve(ledger, *args, **kwargs):
        value = original_reserve(ledger, *args, **kwargs)
        admissions.append(
            {
                "run_id": ledger.run_id,
                "committed_monotonic": time.monotonic(),
                "action_id": value["id"],
            }
        )
        active_model[:] = [ledger.run_id]
        return value

    def settle(ledger, reservation, actual):
        value = original_settle(ledger, reservation, actual)
        settlements.append(
            {"run_id": ledger.run_id, "committed_monotonic": time.monotonic()}
        )
        return value

    def transport(*args, **kwargs):
        if active_model == [first_id]:
            time.sleep(predecessor_hold_seconds)
        return original_transport(*args, **kwargs)

    monkeypatch.setattr(Ledger, "reserve", reserve)
    monkeypatch.setattr(Ledger, "settle", settle)
    monkeypatch.setattr(sdk.GuardedLLM, "_transport_call", transport)
    saved = checkpoint_binding(checkpoints.yielded[0])
    accounting_before = background_accounting(store)
    start = len(checkpoints.selected)
    Worker(checkpoints.settings).run(once=True)
    Worker(checkpoints.settings).run(once=True)
    assert checkpoints.selected[start:] == [first_id, target["id"]]
    assert [item["run_id"] for item in admissions] == [first_id, target["id"]]
    assert [item["run_id"] for item in settlements] == [first_id, target["id"]]
    assert (
        after
        <= admissions[0]["committed_monotonic"]
        <= settlements[0]["committed_monotonic"]
        <= admissions[1]["committed_monotonic"]
    )
    for identifier in (first_id, target["id"]):
        final = store.get(identifier)
        assert final["state"] == "completed_verified", final["reason"]
        assert final["usage"]["calls"] == 1 and final["usage"]["unknown_calls"] == 0
        assert final["usage"]["reserved_tokens"] == 0
        assert not store.pending(identifier)
    assert saved == checkpoint_binding(store.get(saved["id"]))
    assert accounting_before == background_accounting(store)
    print(
        "worker_wait_with_preceding_foreground="
        + json.dumps(
            {
                "queue_class": "one_preceding_foreground",
                "target_run_id": target["id"],
                "predecessor_run_id": first_id,
                "enqueue_commit_bracket_monotonic": [before, after],
                "predecessor_synthetic_provider_hold_seconds": predecessor_hold_seconds,
                "admissions": admissions,
                "settlements": settlements,
                "submit_to_target_first_admission_seconds": {
                    "lower": admissions[1]["committed_monotonic"] - after,
                    "upper": admissions[1]["committed_monotonic"] - before,
                },
                "background_checkpoint_preserved": True,
                "background_accounting_preserved": True,
                "real_model_calls": 0,
                "normal_wait_sla": False,
            },
            sort_keys=True,
        )
    )

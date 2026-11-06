"""Finite GitHub import contracts: real disposable PostgreSQL, synthetic HTTP.

No provider, upstream command, persistent product database or real GitHub is used.
"""

import base64
import copy
import hashlib
import json
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from doonex import discovery
from doonex.api import create_app
from doonex.config import Settings
from doonex.discovery import (
    API_VERSION,
    BLOB_BYTES,
    MAX_BYTES,
    MAX_REQUESTS,
    MIT_BODY,
    TREE_BYTES,
    Discovery,
    DiscoveryError,
    DiscoveryInput,
    ImportInput,
    PreviewInput,
    tree_entries,
)
from doonex.skills import SkillError, prepare_bytes
from doonex.tasksets import TaskSetRegistry
from doonex.workspace import FileContent
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from pydantic import ValidationError

MIT = (
    "MIT License\n\nCopyright (c) 2026 Controlled Fixture Authors\n\n" + MIT_BODY + "\n"
).encode()
APACHE = Path(__file__).with_name("fixtures").joinpath("apache-2.0.txt").read_bytes()


def object_sha(kind, content):
    # Independent fixture construction, including the Git object header.
    return hashlib.sha1(
        (kind + " " + str(len(content))).encode() + b"\0" + content,
        usedforsecurity=False,
    ).hexdigest()


class PublicGit:
    def __init__(self, files=None, prefix="skills/example"):
        self.files = files or {
            "SKILL.md": FileContent(
                b"---\nname: controlled\nlicense: LICENSE.txt\n---\nRead the reference; do not execute assets.\n",
                0o644,
            ),
            "LICENSE.txt": FileContent(MIT, 0o644),
            "scripts/manual.sh": FileContent(
                b"#!/bin/sh\nprintf 'must never execute'\n", 0o755
            ),
            "assets/image.bin": FileContent(b"\x00\xffbounded-binary", 0o644),
        }
        self.prefix, self.blobs, self.trees = prefix, {}, {}
        root = {}
        for path, content in self.files.items():
            full_path = "/".join(part for part in (prefix, path) if part)
            cursor = root
            parts = full_path.split("/")
            for part in parts[:-1]:
                cursor = cursor.setdefault(part, {})
            cursor[parts[-1]] = content

        def tree(node):
            entries = []
            for name, value in node.items():
                if isinstance(value, dict):
                    sha = tree(value)
                    entries.append(
                        {"path": name, "mode": "040000", "type": "tree", "sha": sha}
                    )
                else:
                    sha = object_sha("blob", value.data)
                    self.blobs[sha] = value.data
                    entries.append(
                        {
                            "path": name,
                            "mode": "100755" if value.mode == 0o755 else "100644",
                            "type": "blob",
                            "sha": sha,
                            "size": len(value.data),
                        }
                    )
            ordered = sorted(
                entries,
                key=lambda entry: (
                    entry["path"].encode() + (b"/" if entry["type"] == "tree" else b"")
                ),
            )
            raw = b"".join(
                ("40000" if entry["type"] == "tree" else entry["mode"]).encode()
                + b" "
                + entry["path"].encode()
                + b"\0"
                + bytes.fromhex(entry["sha"])
                for entry in ordered
            )
            sha = object_sha("tree", raw)
            self.trees[sha] = entries
            return sha

        self.root = tree(root)
        self.commit = object_sha(
            "commit", b"tree " + self.root.encode() + b"\n\ncontrolled fixture\n"
        )
        self.calls = []
        self.override = None
        self.before = None
        self.transport = httpx.MockTransport(self.respond)

    def entries(self, sha, recursive):
        result = []
        for entry in self.trees[sha]:
            result.append(copy.deepcopy(entry))
            if recursive and entry["type"] == "tree":
                result.extend(
                    {**child, "path": entry["path"] + "/" + child["path"]}
                    for child in self.entries(entry["sha"], True)
                )
        return result

    @staticmethod
    def response(value, status=200, headers=None):
        return httpx.Response(
            status, headers=headers, stream=httpx.ByteStream(json.dumps(value).encode())
        )

    def respond(self, request):
        assert request.method == "GET"
        assert request.url.scheme == "https" and request.url.host == "api.github.com"
        assert request.headers["X-GitHub-Api-Version"] == API_VERSION
        assert request.headers["accept-encoding"] == "identity"
        assert not any(
            key in request.headers
            for key in ("authorization", "cookie", "proxy-authorization")
        )
        self.calls.append(request)
        if self.before:
            self.before(request)
        path = request.url.path
        if path == "/repos/fixture/public":
            value = {
                "id": 123,
                "full_name": "fixture/public",
                "private": False,
                "visibility": "public",
            }
        elif path.startswith("/repos/fixture/public/commits/"):
            value = {"sha": self.commit, "commit": {"tree": {"sha": self.root}}}
        elif path.startswith("/repos/fixture/public/git/trees/"):
            sha = path.rsplit("/", 1)[-1]
            value = {
                "sha": sha,
                "truncated": False,
                "tree": self.entries(sha, bool(request.url.query)),
            }
        elif path.startswith("/repos/fixture/public/git/blobs/"):
            sha = path.rsplit("/", 1)[-1]
            raw = self.blobs[sha]
            value = {
                "sha": sha,
                "size": len(raw),
                "encoding": "base64",
                "content": base64.b64encode(raw).decode(),
            }
        else:
            pytest.fail("Unexpected endpoint: " + str(request.url))
        if self.override:
            result = self.override(request, value)
            if isinstance(result, httpx.Response):
                return result
            if result is not None:
                value = result
        return self.response(
            value,
            headers={"x-ratelimit-remaining": "42", "x-ratelimit-reset": "1900000000"},
        )


@pytest.fixture(autouse=True)
def no_private_inputs(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Discovery must not inspect model credentials or task sets")

    monkeypatch.setattr(Settings, "credential", forbidden)
    monkeypatch.setattr(TaskSetRegistry, "inspect", forbidden)


@pytest.fixture
def public_git():
    return PublicGit()


@pytest.fixture
def service(settings, store, public_git):
    return Discovery(settings, store, transport=public_git.transport)


def scan(service, repository=None):
    return service.create(
        DiscoveryInput(
            repository="fixture/public",
            ref="main",
            prefix=repository.prefix if repository else "skills/example",
        )
    )


def preview(service, receipt, candidate=0):
    return service.preview(
        receipt["id"],
        PreviewInput(
            candidate_id=receipt["candidates"][candidate]["id"],
            expected_revision=receipt["revision"],
        ),
    )


def import_body(receipt, **extra):
    return ImportInput(
        candidate_id=receipt["preview"]["candidate_id"],
        expected_revision=receipt["revision"],
        preview_sha256=receipt["preview"]["sha256"],
        confirm_source=True,
        confirm_license=True,
        **extra,
    )


def mutate_receipt(service, identifier, mutate):
    with service.store.connect() as connection:
        value = service._load(connection, identifier, lock=True)
        mutate(value)
        service._save(connection, value)


def test_complete_flow_freezes_objects_modes_receipt_and_review(
    service, public_git, monkeypatch
):
    def forbidden(*args, **kwargs):
        pytest.fail("Upstream commands must never execute")

    monkeypatch.setattr("subprocess.run", forbidden)
    initial = scan(service)
    assert initial["status"] == "ready" and len(initial["candidates"]) == 1
    assert initial["source"]["commit"] == public_git.commit
    assert [part["path"] for part in initial["source"]["ancestors"]] == [
        "",
        "skills",
        "skills/example",
    ]
    assert initial["budget"]["requests_used"] == 6
    assert initial["source_rate_limit"] == {"remaining": 42, "reset": 1900000000}
    assert "relative_path" not in initial["candidates"][0]
    # A moved branch is irrelevant: preview uses only saved tree/blob objects.
    public_git.commit = "f" * 40
    displayed = preview(service, initial)
    assert displayed["preview"]["license"]["spdx"] == "MIT"
    assert displayed["preview"]["importable"]
    assert len(displayed["preview"]["files"]) == 4
    assert displayed["preview"]["instructions"].startswith("---\nname:")
    assert "bounded-binary" not in json.dumps(displayed)
    manifest = {entry["path"]: entry for entry in displayed["preview"]["files"]}
    assert manifest["scripts/manual.sh"]["mode"] == 0o755
    assert (
        manifest["assets/image.bin"]["sha256"]
        == hashlib.sha256(public_git.files["assets/image.bin"].data).hexdigest()
    )
    assert not any("/commits/" in request.url.path for request in public_git.calls[6:])
    body = import_body(
        displayed, scope=["python", "python"], permissions=["workspace.read"]
    )
    call_count = len(public_git.calls)
    result = service.import_skill(initial["id"], body)
    record = result["skill"]
    assert record["review_status"] == "pending" and record["review_history"] == []
    assert record["integrity"]["valid"] and result["discovery"]["status"] == "imported"
    assert record["metadata"]["source"].endswith(
        initial["source"]["commit"] + "/skills/example"
    )
    assert record["metadata"]["license_status"] == "declared"
    assert record["metadata"]["license_text"] == MIT.decode()
    assert (
        stat.S_IMODE(
            Path(record["context_path"]).joinpath("scripts/manual.sh").stat().st_mode
        )
        == 0o755
    )
    with pytest.raises(ValueError, match="批准"):
        service.library.freeze_skill(record["version_id"])
    approved = service.library.review(
        record["version_id"], "approved", "controlled manual review"
    )
    frozen = service.library.freeze_skill(record["version_id"])
    assert frozen["version_id"] == record["version_id"]
    repeated = service.import_skill(initial["id"], body)["skill"]
    assert repeated == approved
    rejected = service.library.review(record["version_id"], "rejected", "later review")
    assert service.import_skill(initial["id"], body)["skill"] == rejected
    with pytest.raises(ValueError, match="批准"):
        service.library.freeze_skill(record["version_id"])
    assert len(public_git.calls) == call_count
    assert service.get(initial["id"])["imported"]["version_id"] == record["version_id"]
    assert len(service.list()) == 1


@pytest.mark.parametrize(
    "license_bytes,expected", [(MIT, "MIT"), (APACHE, "Apache-2.0")]
)
def test_complete_supported_licenses(settings, store, license_bytes, expected):
    source = PublicGit(
        {
            "SKILL.md": FileContent(b"Use the fixed instructions.\n", 0o644),
            "LICENSE": FileContent(license_bytes, 0o644),
        },
        prefix="",
    )
    service = Discovery(settings, store, transport=source.transport)
    displayed = preview(service, scan(service, source))
    assert displayed["preview"]["license"]["spdx"] == expected
    assert (
        service.import_skill(displayed["id"], import_body(displayed))["skill"][
            "review_status"
        ]
        == "pending"
    )


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"LICENSE.txt": None}, "candidate_license_missing"),
        ({"LICENSE.txt": b"MIT"}, "unknown_or_conflicting_license_text"),
        (
            {
                "LICENSE.txt": b"Copyright 2026 Example\nUse only for research.\n"
                + MIT_BODY.encode()
            },
            "unknown_or_conflicting_license_text",
        ),
        ({"NOTICE": b"Other notice"}, "license_scope_ambiguous"),
        ({"nested/LICENSE": MIT}, "license_scope_ambiguous"),
        ({"nested/SKILL.md": b"Another skill"}, "nested_skill_boundary"),
        (
            {"asset.c": b"// SPDX-License-Identifier: GPL-3.0-only\n"},
            "conflicting_file_license",
        ),
        (
            {"SKILL.md": b"---\nlicense: Proprietary\n---\nInstructions"},
            "restrictive_file_declaration",
        ),
        (
            {"SKILL.md": b"---\nlicense: Custom terms\n---\nInstructions"},
            "conflicting_file_license",
        ),
        (
            {"other.py": b"# Licensed under the Apache-2.0 license\n"},
            "conflicting_file_license",
        ),
        ({"other.txt": b"Non-commercial use only"}, "restrictive_file_declaration"),
        ({"LICENSE.txt": b"\xff"}, "license_not_utf8"),
    ],
)
def test_ambiguous_licenses_never_enter_library(settings, store, change, reason):
    files = {
        "SKILL.md": FileContent(b"Instructions\n", 0o644),
        "LICENSE.txt": FileContent(MIT, 0o644),
    }
    for path, data in change.items():
        if data is None:
            files.pop(path, None)
        else:
            files[path] = FileContent(data, 0o644)
    source = PublicGit(files)
    service = Discovery(settings, store, transport=source.transport)
    displayed = preview(service, scan(service, source))
    assert not displayed["preview"]["importable"]
    assert reason in displayed["preview"]["license"]["reasons"]
    with pytest.raises(DiscoveryError, match="license_preview_only"):
        service.import_skill(displayed["id"], import_body(displayed))
    assert service.library.list("skill") == []


@pytest.mark.parametrize(
    "path,mode,kind",
    [
        ("../outside", "100644", "blob"),
        ("/absolute", "100644", "blob"),
        (".env", "100644", "blob"),
        ("credentials.json", "100644", "blob"),
        ("a\\b", "100644", "blob"),
        ("a:b", "100644", "blob"),
        ("safe", "120000", "blob"),
        ("safe", "160000", "commit"),
        ("safe", "100664", "blob"),
        ("safe", "100644", "tree"),
    ],
)
def test_tree_unsafe_paths_and_modes(path, mode, kind):
    response = {
        "sha": "a" * 40,
        "truncated": False,
        "tree": [
            {"path": path, "mode": mode, "type": kind, "sha": "b" * 40, "size": 1}
        ],
    }
    with pytest.raises(DiscoveryError):
        tree_entries(response, "a" * 40, recursive=True)


@pytest.mark.parametrize(
    "paths", [("A", "a"), ("é", "e\u0301"), ("a", "a"), ("a", "a/b")]
)
def test_tree_collisions_rejected_before_hash_or_overwrite(paths):
    response = {
        "sha": "a" * 40,
        "truncated": False,
        "tree": [
            {"path": path, "mode": "100644", "type": "blob", "sha": "b" * 40, "size": 1}
            for path in paths
        ],
    }
    with pytest.raises(
        DiscoveryError, match="tree_path_collision|tree_parent_mismatch"
    ):
        tree_entries(response, "a" * 40, recursive=True)


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda value: value.update(truncated=True), "incomplete_or_changed_tree"),
        (lambda value: value.update(sha="f" * 40), "incomplete_or_changed_tree"),
        (lambda value: value["tree"][0].update(sha="f" * 40), "git_tree_hash_mismatch"),
        (
            lambda value: value["tree"][0].update(sha="f" * 64),
            "unsupported_git_object_id",
        ),
        (lambda value: value.update(tree=value["tree"] * 2001), "tree_entry_limit"),
    ],
)
def test_tree_complete_binary_object_is_verified(public_git, mutation, code):
    value = {
        "sha": public_git.root,
        "truncated": False,
        "tree": public_git.entries(public_git.root, True),
    }
    mutation(value)
    with pytest.raises(DiscoveryError, match=code):
        tree_entries(value, public_git.root, recursive=True)


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda value: value.update(sha="f" * 40), "blob_identity_mismatch"),
        (lambda value: value.update(size=value["size"] + 1), "blob_identity_mismatch"),
        (
            lambda value: value.update(content=base64.b64encode(b"wrong").decode()),
            "git_blob_hash_mismatch",
        ),
        (lambda value: value.update(content="not base64!"), "invalid_blob_encoding"),
        (lambda value: value.update(encoding="utf-8"), "blob_identity_mismatch"),
    ],
)
def test_blob_identity_encoding_and_bytes_verified(service, public_git, mutation, code):
    receipt = scan(service)

    def change(request, value):
        if "/git/blobs/" in request.url.path:
            mutation(value)
        return value

    public_git.override = change
    with pytest.raises(DiscoveryError, match=code):
        preview(service, receipt)
    assert service.get(receipt["id"])["status"] == "failed"
    assert service.library.list("skill") == []


@pytest.mark.parametrize("status", [301, 302, 307, 401, 403, 404, 429, 500, 503])
def test_http_failure_has_no_redirect_retry_or_credential_fallback(
    service, public_git, status
):
    public_git.override = lambda request, value: PublicGit.response(
        {"message": "SECRET_HTTP_BODY"},
        status,
        {"Location": "https://evil.invalid/secret", "x-ratelimit-remaining": "0"},
    )
    with pytest.raises(DiscoveryError, match="github_http_" + str(status)) as failure:
        scan(service)
    receipt = service.get(failure.value.discovery_id)
    assert receipt["status"] == "failed" and len(public_git.calls) == 1
    assert receipt["requests"][0]["http_status"] == status
    assert receipt["requests"][0]["status"] == "unknown"
    assert receipt["budget"]["bytes_reserved"] >= TREE_BYTES
    assert "SECRET_HTTP_BODY" not in json.dumps(receipt)


@pytest.mark.parametrize(
    "body,headers,code",
    [
        (b"not JSON secret", {}, "invalid_github_json"),
        (b"[]", {}, "invalid_github_json"),
        (b"{}", {"content-encoding": "gzip"}, "unsupported_response_encoding"),
        (b"x" * (TREE_BYTES + 1), {}, "response_size_limit"),
    ],
)
def test_bounded_http_body_and_encoding(service, public_git, body, headers, code):
    public_git.override = lambda request, value: httpx.Response(
        200, headers=headers, stream=httpx.ByteStream(body)
    )
    with pytest.raises(DiscoveryError, match=code) as failure:
        scan(service)
    receipt = service.get(failure.value.discovery_id)
    assert (
        len(public_git.calls) == 1 and receipt["budget"]["bytes_reserved"] >= TREE_BYTES
    )
    assert receipt["budget"]["bytes_used"] == 0


class InterruptedStream(httpx.SyncByteStream):
    def __iter__(self):
        yield b"partial response"
        raise httpx.ReadError("SECRET_TRANSPORT_DIAGNOSTIC")


def test_partial_response_unknown_reservation_survives_restart_and_get(
    service, public_git, settings
):
    public_git.override = lambda request, value: httpx.Response(
        200, stream=InterruptedStream()
    )
    with pytest.raises(DiscoveryError, match="github_transport_failed") as failure:
        scan(service)
    first = service.get(failure.value.discovery_id)
    restarted = Discovery(
        settings,
        service.store,
        transport=httpx.MockTransport(
            lambda request: pytest.fail("GET must never dispatch HTTP")
        ),
    )
    assert restarted.get(first["id"]) == first
    assert restarted.list() == [first]
    assert first["budget"]["requests_used"] == 1
    assert first["requests"][0]["received_bytes"] == len(b"partial response")
    assert first["budget"]["bytes_reserved"] >= TREE_BYTES
    assert "SECRET_TRANSPORT_DIAGNOSTIC" not in json.dumps(first)


def test_request_intent_committed_before_http_without_row_lock(service, public_git):
    def boundary(request):
        with service.store.connect() as connection:
            rows = connection.execute(
                "SELECT data FROM skill_discoveries FOR UPDATE NOWAIT"
            ).fetchall()
            assert len(rows) == 1
            value = rows[0]["data"]
            assert value["requests"][-1]["status"] == "reserved"
            assert value["requests"][-1]["reserved_bytes"] >= TREE_BYTES

    public_git.before = boundary
    assert scan(service)["status"] == "ready"


@pytest.mark.parametrize("limit", ["requests", "bytes", "deadline"])
def test_saved_limits_refuse_before_next_network(service, public_git, limit):
    receipt = scan(service)

    def exhaust(value):
        if limit == "requests":
            while len(value["requests"]) < MAX_REQUESTS:
                value["requests"].append(
                    {**value["requests"][0], "sequence": len(value["requests"]) + 1}
                )
        elif limit == "bytes":
            value["requests"][0].update(status="unknown", reserved_bytes=MAX_BYTES)
        else:
            value["operation"] = {
                "id": "stale",
                "kind": "scan",
                "deadline_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            }
            value["status"] = "scanning"

    mutate_receipt(service, receipt["id"], exhaust)
    receipt = service.get(receipt["id"])
    old_count = len(public_git.calls)
    with pytest.raises(DiscoveryError, match="budget_exhausted|not_available"):
        preview(service, receipt)
    assert len(public_git.calls) == old_count
    if limit == "deadline":
        assert service.get(receipt["id"])["status"] == "interrupted"


def test_expired_reserved_operation_is_visible_and_never_replayed(
    service, public_git, settings
):
    receipt = scan(service)

    def crash(value):
        value["status"] = "previewing"
        value["operation"] = {
            "id": "crashed",
            "kind": "preview",
            "deadline_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        }
        value["requests"].append(
            {
                "sequence": len(value["requests"]) + 1,
                "kind": "blob",
                "status": "reserved",
                "reserved_bytes": BLOB_BYTES,
                "received_bytes": 0,
                "http_status": None,
                "error": None,
            }
        )

    mutate_receipt(service, receipt["id"], crash)
    restarted = Discovery(
        settings,
        service.store,
        transport=httpx.MockTransport(lambda request: pytest.fail("No replay")),
    )
    saved = restarted.get(receipt["id"])
    assert (
        saved["status"] == "interrupted"
        and saved["requests"][-1]["status"] == "unknown"
    )
    assert saved["budget"]["bytes_reserved"] == BLOB_BYTES
    assert restarted.get(receipt["id"]) == saved


def test_revision_preview_digest_and_one_consumed_intent(service):
    first = scan(service)
    displayed = preview(service, first)
    with pytest.raises(DiscoveryError, match="revision_conflict"):
        preview(service, first)
    bad = import_body(displayed).model_copy(update={"preview_sha256": "0" * 64})
    with pytest.raises(DiscoveryError, match="preview_digest_mismatch"):
        service.import_skill(displayed["id"], bad)
    assert service.library.list("skill") == []


@pytest.mark.parametrize("stage", ["after_persist", "after_bind", "receipt_save"])
def test_filesystem_database_failure_never_selects_or_claims_imported(
    service, monkeypatch, stage
):
    displayed = preview(service, scan(service))
    original_persist, original_bind, original_save = (
        service.library.skills.persist,
        service.library.bind,
        service._save,
    )

    def fail_persist(prepared):
        original_persist(prepared)
        raise RuntimeError("synthetic failure after publication")

    def fail_bind(*args, **kwargs):
        original_bind(*args, **kwargs)
        raise RuntimeError("synthetic failure inside bind transaction")

    def fail_save(connection, value):
        if value["status"] == "imported":
            raise RuntimeError("synthetic receipt write failure")
        return original_save(connection, value)

    monkeypatch.setattr(
        service.library.skills,
        "persist",
        fail_persist if stage == "after_persist" else original_persist,
    )
    monkeypatch.setattr(
        service.library, "bind", fail_bind if stage == "after_bind" else original_bind
    )
    monkeypatch.setattr(
        service, "_save", fail_save if stage == "receipt_save" else original_save
    )
    with pytest.raises(DiscoveryError, match="discovery_validation_failed"):
        service.import_skill(displayed["id"], import_body(displayed))
    assert service.get(displayed["id"])["status"] == "failed"
    assert service.get(displayed["id"])["imported"] is None
    assert service.library.list("skill") == []
    assert len(list(service.library.skills.versions.iterdir())) == 1


def test_committed_import_reply_loss_returns_original_on_same_intent(service):
    displayed = preview(service, scan(service))
    body = import_body(displayed)

    def commit_then_disconnect():
        service.import_skill(displayed["id"], body)
        raise BrokenPipeError("synthetic response lost after committed import")

    with pytest.raises(BrokenPipeError):
        commit_then_disconnect()
    original = service.library.list("skill")[0]
    restarted = Discovery(
        service.settings,
        service.store,
        transport=httpx.MockTransport(
            lambda request: pytest.fail("Import retry must be local")
        ),
    )
    recovered = restarted.import_skill(displayed["id"], body)
    assert recovered["discovery"]["status"] == "imported"
    assert {key: recovered["skill"][key] for key in original} == original
    with pytest.raises(DiscoveryError, match="already_consumed"):
        restarted.import_skill(
            displayed["id"], body.model_copy(update={"scope": ["changed"]})
        )


def test_concurrent_import_one_claim_one_result(service, monkeypatch):
    displayed = preview(service, scan(service))
    body = import_body(displayed)
    arrived, release = threading.Event(), threading.Event()
    persist = service.library.skills.persist

    def slow(prepared):
        arrived.set()
        assert release.wait(10)
        return persist(prepared)

    monkeypatch.setattr(service.library.skills, "persist", slow)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.import_skill, displayed["id"], body)
        assert arrived.wait(10)
        with pytest.raises(DiscoveryError, match="revision_conflict|not_available"):
            service.import_skill(
                displayed["id"], body.model_copy(update={"scope": ["another"]})
            )
        release.set()
        result = first.result(10)
    assert service.import_skill(displayed["id"], body)["skill"] == result["skill"]
    assert len(service.library.list("skill")) == 1


def test_distinct_receipts_same_version_keep_first_library_binding(service):
    first = preview(service, scan(service))
    record = service.import_skill(first["id"], import_body(first))["skill"]
    approved = service.library.review(record["version_id"], "approved", "first review")
    second = preview(service, scan(service))
    result = service.import_skill(second["id"], import_body(second))
    assert result["skill"] == approved
    assert len(service.library.list("skill")) == 1 and len(service.list()) == 2


def test_tampered_report_timestamp_cannot_rebind_or_repair(service):
    first = preview(service, scan(service))
    record = service.import_skill(first["id"], import_body(first))["skill"]
    path = Path(record["context_path"]).parent / "report.json"
    value = json.loads(path.read_text())
    value["created_at"] = "tampered timestamp"
    path.write_text(json.dumps(value))
    changed = path.read_bytes()
    second = preview(service, scan(service))
    with pytest.raises(DiscoveryError, match="bound_skill_integrity_failed"):
        service.import_skill(second["id"], import_body(second))
    assert path.read_bytes() == changed
    assert (
        service.library.get("skill", record["version_id"])["report_sha256"]
        == record["report_sha256"]
    )
    with pytest.raises(DiscoveryError, match="bound_skill_integrity_failed"):
        service.import_skill(first["id"], import_body(first))


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository", "https://evil.invalid/repo"),
        ("repository", "a/../b"),
        ("ref", "../main"),
        ("ref", "main\n"),
        ("prefix", "../private"),
        ("prefix", "a/" * 9 + "b"),
    ],
)
def test_input_locations_are_separate_bounded_not_urls(field, value):
    with pytest.raises(ValidationError):
        DiscoveryInput.model_validate(
            {"repository": "fixture/public", "ref": "main", field: value}
        )


@pytest.mark.parametrize(
    "extra",
    [
        {"approved": True},
        {"verified": True},
        {"commit": "f" * 40},
        {"download_url": "https://evil.invalid"},
        {"taskset": "private"},
        {"evaluation": "private"},
    ],
)
def test_client_cannot_override_import_source_or_trust(extra):
    with pytest.raises(ValidationError):
        ImportInput.model_validate(
            {
                "candidate_id": "a" * 64,
                "expected_revision": "b" * 64,
                "preview_sha256": "c" * 64,
                "confirm_source": True,
                "confirm_license": True,
                **extra,
            }
        )


@pytest.mark.parametrize("value", [False, 1, "true", None])
def test_import_confirmation_must_be_literal_boolean(value):
    with pytest.raises(ValidationError):
        ImportInput(
            candidate_id="a" * 64,
            expected_revision="b" * 64,
            preview_sha256="c" * 64,
            confirm_source=value,
            confirm_license=True,
        )


def test_ref_is_encoded_as_one_path_segment(service, public_git):
    ref = "feature/a?secret#part%value"
    service.create(
        DiscoveryInput(repository="fixture/public", ref=ref, prefix="skills/example")
    )
    request = next(
        request for request in public_git.calls if "/commits/" in request.url.path
    )
    assert request.url.raw_path.endswith(quote(ref, safe="").encode())
    assert not request.url.query and not request.url.fragment


def test_private_cache_and_byte_preparation_reject_modes(service):
    with pytest.raises(SkillError, match="file modes"):
        prepare_bytes({"SKILL.md": FileContent(b"Instructions", 0o600)}, (), {})
    sha = object_sha("blob", b"bytes")
    identifier = "controlled-cache"
    assert service._cache(identifier, sha, b"bytes") == b"bytes"
    path = service.root / identifier / sha
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)
    with pytest.raises(DiscoveryError, match="cached_blob_integrity_failed"):
        service._cache(identifier, sha)


def test_api_auth_static_routes_safe_errors_and_get_no_network(
    settings, public_git, monkeypatch
):
    original = Discovery.__init__

    def controlled(self, settings, store, **kwargs):
        original(self, settings, store, transport=public_git.transport)

    monkeypatch.setattr(Discovery, "__init__", controlled)
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/skills/discoveries").status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.token
        assert client.get("/api/skills/discoveries").json() == {"discoveries": []}
        assert (
            client.get(
                "/api/skills/discoveries", headers={"Origin": "https://evil.invalid"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/skills/discoveries",
                json={"repository": "fixture/public", "ref": "main", "approved": True},
            ).status_code
            == 422
        )
        response = client.post(
            "/api/skills/discoveries",
            json={
                "repository": "fixture/public",
                "ref": "main",
                "prefix": "skills/example",
            },
        )
        assert response.status_code == 200, response.text
        receipt = response.json()
        count = len(public_git.calls)
        public_git.before = lambda request: pytest.fail(
            "GET routes must not use network"
        )
        assert client.get("/api/skills/discoveries").json()["discoveries"] == [receipt]
        assert client.get("/api/skills/discoveries/" + receipt["id"]).json() == receipt
        invalid = client.get("/api/skills/discoveries/not-a-uuid")
        assert invalid.status_code == 404 and invalid.json() == {
            "detail": {"code": "discovery_not_found"}
        }
        conflict = client.post(
            "/api/skills/discoveries/" + receipt["id"] + "/preview",
            json={
                "candidate_id": receipt["candidates"][0]["id"],
                "expected_revision": "0" * 64,
            },
        )
        assert (
            conflict.status_code == 409
            and conflict.json()["detail"]["code"] == "discovery_revision_conflict"
        )
        assert len(public_git.calls) == count


def test_actual_httpx_options_disable_ambient_auth_proxy_redirect_retry(
    service, public_git, monkeypatch, tmp_path
):
    original = httpx.Client
    options = []

    def client(**kwargs):
        options.append(kwargs)
        return original(**kwargs)

    netrc = tmp_path / "netrc"
    netrc.write_text(
        "machine api.github.com login PRIVATE_USER password PRIVATE_NETRC_SENTINEL\n"
    )
    netrc.chmod(0o600)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("NETRC", str(netrc))
    monkeypatch.setenv("GITHUB_TOKEN", "PRIVATE_GITHUB_SENTINEL")
    monkeypatch.setattr(discovery.httpx, "Client", client)
    assert scan(service)["status"] == "ready"
    assert len(options) == len(public_git.calls)
    assert all(
        option["trust_env"] is False
        and option["follow_redirects"] is False
        and 0 < option["timeout"] <= 10
        and "auth" not in option
        and "cookies" not in option
        for option in options
    )
    assert "PRIVATE_" not in str(
        [(str(request.url), dict(request.headers)) for request in public_git.calls]
    )


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda value: value.update(id=True), "repository_not_same_public_identity"),
        (lambda value: value.update(id=0), "repository_not_same_public_identity"),
        (
            lambda value: value.update(private=True),
            "repository_not_same_public_identity",
        ),
        (
            lambda value: value.update(visibility="private"),
            "repository_not_same_public_identity",
        ),
        (
            lambda value: value.update(full_name="other/repository"),
            "repository_not_same_public_identity",
        ),
    ],
)
def test_repository_must_be_same_public_identity(service, public_git, change, code):
    def modify(request, value):
        if request.url.path == "/repos/fixture/public":
            change(value)
        return value

    public_git.override = modify
    with pytest.raises(DiscoveryError, match=code):
        scan(service)
    assert len(public_git.calls) == 1


def test_fixed_full_commit_ref_rejects_another_valid_commit(service, public_git):
    with pytest.raises(DiscoveryError, match="commit_identity_mismatch"):
        service.create(
            DiscoveryInput(
                repository="fixture/public", ref="f" * 40, prefix="skills/example"
            )
        )
    assert len(public_git.calls) == 2
    assert scan(service)["status"] == "ready"


def test_deep_entry_rejected_during_scan(settings, store):
    source = PublicGit(
        {
            "SKILL.md": FileContent(b"Instruction", 0o644),
            "a/b/c/d/e/f/g/h/i/j/file": FileContent(b"small", 0o644),
        },
        prefix="",
    )
    service = Discovery(settings, store, transport=source.transport)
    with pytest.raises(DiscoveryError, match="tree_depth_limit"):
        scan(service, source)
    assert service.library.list("skill") == []


def test_preview_final_public_identity_drift_fails(service, public_git):
    receipt = scan(service)

    def drift(request, value):
        if request.url.path == "/repos/fixture/public":
            value["id"] = 456
        return value

    public_git.override = drift
    with pytest.raises(DiscoveryError, match="repository_identity_changed"):
        preview(service, receipt)
    assert service.get(receipt["id"])["preview"] is None


def test_strict_blob_size_disallows_json_boolean(settings, store):
    source = PublicGit(
        {"SKILL.md": FileContent(b"x", 0o644), "LICENSE": FileContent(MIT, 0o644)}
    )
    service = Discovery(settings, store, transport=source.transport)
    receipt = scan(service, source)

    def bad_type(request, value):
        if "/git/blobs/" in request.url.path and value["size"] == 1:
            value["size"] = True
        return value

    source.override = bad_type
    with pytest.raises(DiscoveryError, match="blob_identity_mismatch"):
        preview(service, receipt)


def test_claim_crash_preserves_unknown_and_never_replays(
    service, public_git, monkeypatch, settings
):
    receipt = scan(service)
    original = service.reserve

    def crash(*args):
        original(*args)
        raise SystemExit("synthetic process exit after committed intent")

    monkeypatch.setattr(service, "reserve", crash)
    count = len(public_git.calls)
    with pytest.raises(SystemExit):
        preview(service, receipt)
    restarted = Discovery(
        settings,
        service.store,
        transport=httpx.MockTransport(lambda request: pytest.fail("Do not replay")),
    )
    saved = restarted.get(receipt["id"])
    assert (
        saved["status"] == "previewing"
        and saved["requests"][-1]["status"] == "reserved"
    )
    assert saved["budget"]["bytes_reserved"] >= BLOB_BYTES
    assert len(public_git.calls) == count
    with pytest.raises(DiscoveryError, match="not_available"):
        preview(restarted, saved)


def test_content_validation_occurs_outside_database_transactions(service, monkeypatch):
    displayed = preview(service, scan(service))
    original = service.library.skill
    calls = []

    def validated(record, **kwargs):
        with service.store.connect() as connection:
            rows = connection.execute(
                "SELECT data FROM skill_discoveries FOR UPDATE NOWAIT"
            ).fetchall()
            assert rows and rows[0]["data"]["status"] == "importing"
        calls.append(record["version_id"])
        return original(record, **kwargs)

    monkeypatch.setattr(service.library, "skill", validated)
    assert service.import_skill(displayed["id"], import_body(displayed))["skill"][
        "integrity"
    ]["valid"]
    assert len(calls) == 1


def test_same_version_parallel_receipts_preserve_first_binding(service, monkeypatch):
    left, right = preview(service, scan(service)), preview(service, scan(service))
    original = service.library.skills.persist
    barrier = threading.Barrier(2)

    def publish(prepared):
        result = original(prepared)
        barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(service.library.skills, "persist", publish)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(service.import_skill, value["id"], import_body(value))
            for value in (left, right)
        ]
        results = [future.result(15) for future in futures]
    assert results[0]["skill"] == results[1]["skill"]
    assert all(result["discovery"]["status"] == "imported" for result in results)
    assert len(service.library.list("skill")) == 1


def test_api_preview_and_pending_import_preserve_discovery_boundary(
    settings, public_git, monkeypatch
):
    original = Discovery.__init__

    def controlled(self, settings, store, **kwargs):
        original(self, settings, store, transport=public_git.transport)

    monkeypatch.setattr(Discovery, "__init__", controlled)
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        created = client.post(
            "/api/skills/discoveries",
            json={
                "repository": "fixture/public",
                "ref": "main",
                "prefix": "skills/example",
            },
        ).json()
        url = "/api/skills/discoveries/" + created["id"]
        response = client.post(
            url + "/preview",
            json={
                "candidate_id": created["candidates"][0]["id"],
                "expected_revision": created["revision"],
            },
        )
        assert response.status_code == 200, response.text
        displayed = response.json()
        response = client.post(
            url + "/import", json=import_body(displayed).model_dump()
        )
        assert response.status_code == 200, response.text
        imported = response.json()
        assert imported["skill"]["review_status"] == "pending"
        assert (
            client.get(url).json()["imported"]["version_id"]
            == imported["skill"]["version_id"]
        )
        assert (
            client.get("/api/skills/" + imported["skill"]["version_id"]).json()[
                "instructions"
            ]
            == displayed["preview"]["instructions"]
        )


def test_empty_deep_tree_path_is_also_bounded():
    response = {
        "sha": "a" * 40,
        "truncated": False,
        "tree": [
            {
                "path": "a/b/c/d/e/f/g/h/i/j",
                "type": "tree",
                "mode": "040000",
                "sha": object_sha("tree", b""),
            }
        ],
    }
    with pytest.raises(DiscoveryError, match="tree_depth_limit"):
        tree_entries(response, "a" * 40, recursive=True)


def test_post_commit_database_disconnect_preserves_consumed_result(
    service, monkeypatch
):
    displayed = preview(service, scan(service))
    body = import_body(displayed)
    original = service.store.connect
    raised = []

    @contextmanager
    def disconnect():
        committed = False
        with original() as connection:
            yield connection
            row = connection.execute(
                "SELECT data FROM skill_discoveries WHERE id=%s", (displayed["id"],)
            ).fetchone()
            committed = row["data"]["status"] == "imported" and not raised
        if committed:
            raised.append(True)
            raise ConnectionError(
                "synthetic disconnect after commit before acknowledgement"
            )

    monkeypatch.setattr(service.store, "connect", disconnect)
    with pytest.raises(DiscoveryError, match="import_result_already_committed"):
        service.import_skill(displayed["id"], body)
    assert raised and service.get(displayed["id"])["status"] == "imported"
    result = service.import_skill(displayed["id"], body)
    assert (
        result["skill"]["integrity"]["valid"]
        and len(service.library.list("skill")) == 1
    )


def test_receipt_tamper_cannot_revalidate_or_fetch(service, public_git):
    receipt = scan(service)
    with service.store.connect() as connection:
        value = service._load(connection, receipt["id"])
        value["source"]["commit"] = "f" * 40
        connection.execute(
            "UPDATE skill_discoveries SET data=%s WHERE id=%s",
            (Jsonb(value), receipt["id"]),
        )
    count = len(public_git.calls)
    with pytest.raises(DiscoveryError, match="receipt_integrity_failed"):
        service.get(receipt["id"])
    with pytest.raises(DiscoveryError, match="receipt_integrity_failed"):
        preview(service, receipt)
    assert len(public_git.calls) == count


def test_stream_checks_original_saved_operation_deadline(
    service, public_git, monkeypatch
):
    receipt = scan(service)
    original = service._claim

    def short_claim(*args):
        value = original(*args)
        value["operation"]["deadline_at"] = (
            datetime.now(UTC) + timedelta(seconds=0.1)
        ).isoformat()
        with service.store.connect() as connection:
            service._save(connection, value)
        return value

    def slow(request):
        if "/git/blobs/" in request.url.path:
            time.sleep(0.15)

    monkeypatch.setattr(service, "_claim", short_claim)
    public_git.before = slow
    count = len(public_git.calls)
    with pytest.raises(DiscoveryError, match="operation_deadline"):
        preview(service, receipt)
    saved = service.get(receipt["id"])
    assert saved["status"] == "failed" and saved["requests"][-1]["status"] == "unknown"
    assert saved["budget"]["bytes_reserved"] >= BLOB_BYTES
    assert len(public_git.calls) == count + 1


def test_receipt_budget_limits_do_not_increase_with_controller_constants(
    service, monkeypatch
):
    receipt = scan(service)
    original = receipt["budget"]
    monkeypatch.setattr(discovery, "MAX_REQUESTS", MAX_REQUESTS * 10)
    monkeypatch.setattr(discovery, "MAX_BYTES", MAX_BYTES * 10)
    assert service.get(receipt["id"])["budget"] == original

    def exhaust(value):
        while len(value["requests"]) < MAX_REQUESTS:
            value["requests"].append(
                {**value["requests"][0], "sequence": len(value["requests"]) + 1}
            )

    mutate_receipt(service, receipt["id"], exhaust)
    current = service.get(receipt["id"])
    assert current["budget"]["remaining_requests"] == 0
    with pytest.raises(DiscoveryError, match="discovery_budget_exhausted"):
        preview(service, current)


@pytest.mark.parametrize(
    "owner", ["2026 Example Author, Inc.", "2024-2026 Example Contributors"]
)
def test_apache_appendix_identifying_fields_preserve_full_body(owner):
    from doonex.discovery import check_license, complete_apache

    populated = APACHE.replace(b"[yyyy] [name of copyright owner]", owner.encode())
    assert complete_apache(populated.decode())
    files = {
        "SKILL.md": FileContent(
            b"---\nlicense: LICENSE.txt\n---\nRead the guide.\n", 0o644
        ),
        "LICENSE.txt": FileContent(populated, 0o644),
    }
    result = check_license(files)
    assert result["status"] == "supported" and result["spdx"] == "Apache-2.0"
    assert result["files"][0]["text"].encode() == populated


@pytest.mark.parametrize(
    "change",
    [
        "term",
        "missing_section",
        "extra_term",
        "before_appendix",
        "empty_owner",
        "unknown_year",
        "extra_line",
    ],
)
def test_populated_apache_appendix_does_not_relax_any_other_terms(change):
    from doonex.discovery import complete_apache

    text = APACHE.decode().replace(
        "[yyyy] [name of copyright owner]", "2026 Example Author, Inc."
    )
    if change == "term":
        text = text.replace("perpetual", "temporary", 1)
    elif change == "missing_section":
        text = text.replace("3. Grant of Patent License.", "", 1)
    elif change == "extra_term":
        text += "\nUse only for non-commercial research."
    elif change == "before_appendix":
        text = "Copyright 2026 Extra Person\n" + text
    elif change == "empty_owner":
        text = text.replace("2026 Example Author, Inc.", "2026")
    elif change == "unknown_year":
        text = text.replace("2026 Example Author, Inc.", "[yyyy] Example Author, Inc.")
    else:
        text = text.replace(
            "2026 Example Author, Inc.",
            "2026 Example Author, Inc.\nLicense: proprietary",
        )
    assert not complete_apache(text)

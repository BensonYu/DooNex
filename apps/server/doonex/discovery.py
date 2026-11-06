"""Finite, unauthenticated GitHub object reads and pending-only Skill import.

No upstream file is executed. Every HTTP intent reserves a persistent request
slot and worst-case byte allowance before leaving its short transaction.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import time
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException
from psycopg.types.json import Jsonb
from pydantic import Field, field_validator

from doonex.library import Library
from doonex.models import StrictModel
from doonex.skills import (
    MAX_SKILL_DEPTH,
    MAX_SKILL_ENTRIES,
    MAX_SKILL_FILE_BYTES,
    MAX_SKILL_TOTAL_BYTES,
    SkillError,
    _check_name,
    _no_links,
    prepare_bytes,
)
from doonex.store import now
from doonex.workspace import FileContent, WorkspaceError

API_VERSION = "2022-11-28"
ORIGIN = "https://api.github.com"
MAX_REQUESTS = 32
MAX_BYTES = 32 * 1024 * 1024
TREE_BYTES = 2 * 1024 * 1024
BLOB_BYTES = 4 * 1024 * 1024
# Locked httpcore HTTP/1.1 reads at most 64 KiB at a time. Reserve the final
# read before a streaming size rejection, and retain its actual byte count.
READ_ALLOWANCE = 64 * 1024
MAX_ENTRIES = 2000
MAX_CANDIDATES = 20
OBJECT = re.compile(r"[0-9a-f]{40}\Z")
SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")


class DiscoveryError(Exception):
    def __init__(self, code, status=400, discovery_id=None):
        super().__init__(code)
        self.code, self.status, self.discovery_id = code, status, discovery_id


def canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def revision(value):
    return digest({key: item for key, item in value.items() if key != "revision"})


def oid(value):
    if not isinstance(value, str) or OBJECT.fullmatch(value) is None:
        raise DiscoveryError("unsupported_git_object_id")
    return value


def git_hash(kind, data):
    return hashlib.sha1(
        f"{kind} {len(data)}\0".encode() + data, usedforsecurity=False
    ).hexdigest()


class DiscoveryInput(StrictModel):
    repository: str = Field(min_length=3, max_length=201)
    ref: str = Field(min_length=1, max_length=255)
    prefix: str = Field(default="", max_length=1024)

    @field_validator("repository")
    @classmethod
    def repository_name(cls, value):
        parts = value.split("/")
        if len(parts) != 2 or any(
            SEGMENT.fullmatch(part) is None or part in {".", ".."} for part in parts
        ):
            raise ValueError("repository_requires_owner_and_name")
        return value

    @field_validator("ref")
    @classmethod
    def git_ref(cls, value):
        if value.strip() != value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("invalid_git_ref")
        if (
            value.startswith(("/", "."))
            or ".." in value
            or "//" in value
            or "\\" in value
        ):
            raise ValueError("invalid_git_ref")
        return value

    @field_validator("prefix")
    @classmethod
    def relative_prefix(cls, value):
        if value:
            try:
                _check_name(value)
            except (SkillError, WorkspaceError) as error:
                raise ValueError("invalid_relative_prefix") from error
            if len(value.split("/")) > 8:
                raise ValueError("prefix_depth_exceeded")
        return value


class PreviewInput(StrictModel):
    candidate_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class ImportInput(PreviewInput):
    preview_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirm_source: Literal[True]
    confirm_license: Literal[True]
    scope: list[str] = Field(default_factory=list, max_length=100)
    permissions: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("scope", "permissions")
    @classmethod
    def declarations(cls, values):
        if any(
            not value.strip()
            or len(value) > 512
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            for value in values
        ):
            raise ValueError("invalid_skill_declaration")
        return sorted(set(values))

    @field_validator("confirm_source", "confirm_license", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("explicit_confirmation_required")
        return value


def tree_entries(response, expected, *, recursive):
    """Verify paths, types, parent links and each complete Git tree object."""
    if response.get("sha") != expected or response.get("truncated") is not False:
        raise DiscoveryError("incomplete_or_changed_tree")
    raw = response.get("tree")
    if not isinstance(raw, list) or len(raw) > MAX_ENTRIES:
        raise DiscoveryError("tree_entry_limit")
    entries, keys = {}, set()
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise DiscoveryError("invalid_tree_entry")
        path = item["path"]
        if path.count("/") > MAX_SKILL_DEPTH:
            raise DiscoveryError("tree_depth_limit")
        try:
            _check_name(path)
        except (SkillError, WorkspaceError) as error:
            raise DiscoveryError("unsafe_tree_path") from error
        if not recursive and "/" in path:
            raise DiscoveryError("invalid_nonrecursive_tree")
        key = unicodedata.normalize("NFC", path).casefold()
        if key in keys or path in entries:
            raise DiscoveryError("tree_path_collision")
        keys.add(key)
        mode, kind = item.get("mode"), item.get("type")
        if (mode, kind) not in {
            ("040000", "tree"),
            ("100644", "blob"),
            ("100755", "blob"),
        }:
            raise DiscoveryError("unsupported_git_entry_mode")
        if kind == "blob" and (type(item.get("size")) is not int or item["size"] < 0):
            raise DiscoveryError("invalid_blob_size")
        entries[path] = {
            "path": path,
            "mode": mode,
            "type": kind,
            "sha": oid(item.get("sha")),
            **({"size": item["size"]} if kind == "blob" else {}),
        }
    groups = {"": []}
    for path, entry in entries.items():
        parent, _, _name = path.rpartition("/")
        if parent and (parent not in entries or entries[parent]["type"] != "tree"):
            raise DiscoveryError("tree_parent_mismatch")
        groups.setdefault(parent, []).append(entry)
        if entry["type"] == "tree" and recursive:
            groups.setdefault(path, [])
    for parent, children in groups.items():
        ordered = sorted(
            children,
            key=lambda item: (
                item["path"].rsplit("/", 1)[-1].encode()
                + (b"/" if item["type"] == "tree" else b"")
            ),
        )
        content = b"".join(
            ("40000" if item["type"] == "tree" else item["mode"]).encode()
            + b" "
            + item["path"].rsplit("/", 1)[-1].encode()
            + b"\0"
            + bytes.fromhex(item["sha"])
            for item in ordered
        )
        actual = git_hash("tree", content)
        wanted = entries[parent]["sha"] if parent else expected
        if actual != wanted:
            raise DiscoveryError("git_tree_hash_mismatch")
    return entries


class GitHubReader:
    def __init__(
        self, service, discovery_id, operation_id, *, deadline_at, transport=None
    ):
        self.service, self.discovery_id, self.operation_id = (
            service,
            discovery_id,
            operation_id,
        )
        self.transport = transport
        remaining = (
            datetime.fromisoformat(deadline_at) - datetime.now(UTC)
        ).total_seconds()
        self.deadline = time.monotonic() + max(0, min(60, remaining))

    def get(self, suffix, kind, maximum):
        sequence = self.service.reserve(
            self.discovery_id, self.operation_id, kind, maximum + READ_ALLOWANCE
        )
        received, status, rate = 0, None, None
        try:
            remaining = min(10.0, self.deadline - time.monotonic())
            if remaining <= 0:
                raise DiscoveryError("operation_deadline")
            with (
                httpx.Client(
                    transport=self.transport,
                    timeout=remaining,
                    trust_env=False,
                    follow_redirects=False,
                    headers={
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": API_VERSION,
                        "User-Agent": "DooNex-discovery",
                        "Accept-Encoding": "identity",
                    },
                ) as client,
                client.stream("GET", ORIGIN + suffix) as response,
            ):
                status = response.status_code
                rate = {
                    key: int(value) if value and value.isdigit() else None
                    for key, value in (
                        ("remaining", response.headers.get("x-ratelimit-remaining")),
                        ("reset", response.headers.get("x-ratelimit-reset")),
                    )
                }
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise DiscoveryError("unsupported_response_encoding")
                body = bytearray()
                # Coalescing chunks would lose an already received partial
                # chunk's byte count when the following read disconnects.
                for chunk in response.iter_raw():
                    received += len(chunk)
                    if received > maximum:
                        raise DiscoveryError("response_size_limit")
                    if time.monotonic() > self.deadline:
                        raise DiscoveryError("operation_deadline")
                    body.extend(chunk)
                if status != 200:
                    raise DiscoveryError("github_http_" + str(status))
                try:
                    value = json.loads(bytes(body))
                except (ValueError, UnicodeDecodeError) as error:
                    raise DiscoveryError("invalid_github_json") from error
                if not isinstance(value, dict):
                    raise DiscoveryError("invalid_github_json")
            self.service.finish_request(
                self.discovery_id,
                self.operation_id,
                sequence,
                received,
                status,
                rate,
                None,
            )
            return value
        except Exception as error:  # noqa: BLE001 -- remote diagnostics never leave this boundary
            code = (
                error.code
                if isinstance(error, DiscoveryError)
                else "github_transport_failed"
            )
            self.service.finish_request(
                self.discovery_id,
                self.operation_id,
                sequence,
                received,
                status,
                rate,
                code,
            )
            raise DiscoveryError(code, discovery_id=self.discovery_id) from None


class Discovery:
    def __init__(self, settings, store, *, transport=None):
        self.settings, self.store, self.transport = settings, store, transport
        self.library = Library(settings, store)
        self.root = settings.home / "discoveries"
        _no_links(self.root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if stat.S_IMODE(self.root.stat().st_mode) != 0o700:
            raise DiscoveryError("private_discovery_storage_required")

    def _load(self, c, identifier, *, lock=False):
        try:
            identifier = str(UUID(str(identifier)))
        except ValueError as error:
            raise DiscoveryError("discovery_not_found", 404) from error
        row = c.execute(
            "SELECT data FROM skill_discoveries WHERE id=%s"
            + (" FOR UPDATE" if lock else ""),
            (identifier,),
        ).fetchone()
        if not row:
            raise DiscoveryError("discovery_not_found", 404)
        value = row["data"]
        if value.get("revision") != revision(value):
            raise DiscoveryError("discovery_receipt_integrity_failed", 409, identifier)
        return value

    def _save(self, c, value):
        value["updated_at"] = now()
        value["revision"] = revision(value)
        c.execute(
            "UPDATE skill_discoveries SET data=%s WHERE id=%s",
            (Jsonb(value), value["id"]),
        )

    @staticmethod
    def public(value):
        result = {
            key: value[key]
            for key in (
                "id",
                "revision",
                "status",
                "created_at",
                "updated_at",
                "input",
                "source",
                "candidates",
                "preview",
                "imported",
                "error",
                "source_rate_limit",
            )
        }
        used = sum(
            item["received_bytes"]
            for item in value["requests"]
            if item["status"] == "completed"
        )
        reserved = sum(
            item["reserved_bytes"]
            for item in value["requests"]
            if item["status"] != "completed"
        )
        result["budget"] = {
            "max_requests": MAX_REQUESTS,
            "requests_used": len(value["requests"]),
            "remaining_requests": MAX_REQUESTS - len(value["requests"]),
            "max_bytes": MAX_BYTES,
            "bytes_used": used,
            "bytes_reserved": reserved,
            "remaining_bytes": MAX_BYTES - used - reserved,
        }
        result["requests"] = [
            {
                key: item[key]
                for key in (
                    "sequence",
                    "kind",
                    "status",
                    "received_bytes",
                    "http_status",
                    "error",
                )
            }
            for item in value["requests"]
        ]
        result["candidates"] = [
            {
                key: item[key]
                for key in (
                    "id",
                    "path",
                    "tree_sha",
                    "file_count",
                    "entry_count",
                    "total_bytes",
                )
            }
            for item in value["candidates"]
        ]
        operation = value.get("operation")
        if operation and datetime.now(UTC) >= datetime.fromisoformat(
            operation["deadline_at"]
        ):
            result["status"] = "interrupted"
            result["requests"] = [
                {**item, "status": "unknown"} if item["status"] == "reserved" else item
                for item in result["requests"]
            ]
        return result

    def get(self, identifier):
        with self.store.connect() as c:
            return self.public(self._load(c, identifier))

    def list(self):
        with self.store.connect() as c:
            identifiers = [
                row["id"]
                for row in c.execute(
                    "SELECT id FROM skill_discoveries ORDER BY data->>'created_at',id"
                )
            ]
            return [
                self.public(self._load(c, identifier)) for identifier in identifiers
            ]

    def reserve(self, identifier, operation_id, kind, maximum):
        with self.store.connect() as c:
            value = self._load(c, identifier, lock=True)
            operation = value.get("operation") or {}
            if operation.get("id") != operation_id:
                raise DiscoveryError("operation_claim_changed", 409, identifier)
            if datetime.now(UTC) >= datetime.fromisoformat(operation["deadline_at"]):
                raise DiscoveryError("operation_deadline", discovery_id=identifier)
            budget = self.public(value)["budget"]
            if budget["remaining_requests"] < 1 or budget["remaining_bytes"] < maximum:
                raise DiscoveryError(
                    "discovery_budget_exhausted", discovery_id=identifier
                )
            sequence = len(value["requests"]) + 1
            value["requests"].append(
                {
                    "sequence": sequence,
                    "kind": kind,
                    "status": "reserved",
                    "reserved_bytes": maximum,
                    "received_bytes": 0,
                    "http_status": None,
                    "error": None,
                    "started_at": now(),
                }
            )
            self._save(c, value)
        return sequence

    def finish_request(
        self, identifier, operation_id, sequence, received, status, rate, error
    ):
        with self.store.connect() as c:
            value = self._load(c, identifier, lock=True)
            if (value.get("operation") or {}).get("id") != operation_id:
                raise DiscoveryError("operation_claim_changed", 409, identifier)
            item = value["requests"][sequence - 1]
            if item["status"] != "reserved":
                raise DiscoveryError("request_already_settled", 409, identifier)
            item.update(
                status="unknown" if error else "completed",
                received_bytes=received,
                http_status=status,
                error=error,
                ended_at=now(),
            )
            if error:
                item["reserved_bytes"] = max(item["reserved_bytes"], received)
            value["source_rate_limit"] = rate
            self._save(c, value)

    def _claim(self, identifier, body, kind):
        with self.store.connect() as c:
            value = self._load(c, identifier, lock=True)
            if value["revision"] != body.expected_revision:
                raise DiscoveryError("discovery_revision_conflict", 409, identifier)
            if value["status"] not in {"ready", "preview_ready"} or value["operation"]:
                raise DiscoveryError(
                    "discovery_operation_not_available", 409, identifier
                )
            if not any(item["id"] == body.candidate_id for item in value["candidates"]):
                raise DiscoveryError("candidate_not_in_discovery", 409, identifier)
            value["operation"] = {
                "id": str(uuid4()),
                "kind": kind,
                "deadline_at": (datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
            }
            value["status"] = "previewing" if kind == "preview" else "importing"
            self._save(c, value)
            return value

    def _failure(self, identifier, error):
        code = (
            error.code
            if isinstance(error, DiscoveryError)
            else "discovery_validation_failed"
        )
        with self.store.connect() as c:
            value = self._load(c, identifier, lock=True)
            if value["status"] == "imported":
                raise DiscoveryError("import_result_already_committed", 409, identifier)
            for request in value["requests"]:
                if request["status"] == "reserved":
                    request.update(status="unknown", error=code)
            value.update(status="failed", error={"code": code}, operation=None)
            self._save(c, value)
        raise DiscoveryError(
            code, error.status if isinstance(error, DiscoveryError) else 400, identifier
        ) from None

    def create(self, body):
        identifier = str(uuid4())
        value = {
            "id": identifier,
            "input": body.model_dump(),
            "status": "scanning",
            "created_at": now(),
            "updated_at": now(),
            "source": None,
            "candidates": [],
            "preview": None,
            "previews": {},
            "imported": None,
            "requests": [],
            "error": None,
            "source_rate_limit": None,
            "entries": {},
            "operation": {
                "id": str(uuid4()),
                "kind": "scan",
                "deadline_at": (datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
            },
        }
        value["revision"] = revision(value)
        with self.store.connect() as c:
            c.execute(
                "INSERT INTO skill_discoveries VALUES(%s,%s)",
                (identifier, Jsonb(value)),
            )
        reader = GitHubReader(
            self,
            identifier,
            value["operation"]["id"],
            deadline_at=value["operation"]["deadline_at"],
            transport=self.transport,
        )
        prefix = "/repos/" + "/".join(
            quote(part, safe="") for part in body.repository.split("/")
        )
        try:
            repository = reader.get(prefix, "repository", TREE_BYTES)
            if (
                repository.get("private") is not False
                or repository.get("visibility") != "public"
                or type(repository.get("id")) is not int
                or repository["id"] < 1
                or repository.get("full_name", "").casefold()
                != body.repository.casefold()
            ):
                raise DiscoveryError("repository_not_same_public_identity")
            commit = reader.get(
                prefix + "/commits/" + quote(body.ref, safe=""),
                "resolve_ref",
                TREE_BYTES,
            )
            commit_sha = oid(commit.get("sha"))
            if (
                re.fullmatch(r"[a-fA-F0-9]{40}", body.ref)
                and commit_sha != body.ref.lower()
            ):
                raise DiscoveryError("commit_identity_mismatch")
            root = oid(commit.get("commit", {}).get("tree", {}).get("sha"))
            tree = root
            ancestors = [{"path": "", "sha": root}]
            ancestor_path = []
            for segment in body.prefix.split("/") if body.prefix else []:
                entries = tree_entries(
                    reader.get(
                        prefix + "/git/trees/" + tree, "prefix_tree", TREE_BYTES
                    ),
                    tree,
                    recursive=False,
                )
                entry = entries.get(segment)
                if not entry or entry["type"] != "tree":
                    raise DiscoveryError("prefix_not_a_tree")
                tree = entry["sha"]
                ancestor_path.append(segment)
                ancestors.append({"path": "/".join(ancestor_path), "sha": tree})
            entries = tree_entries(
                reader.get(
                    prefix + "/git/trees/" + tree + "?recursive=1",
                    "scan_tree",
                    TREE_BYTES,
                ),
                tree,
                recursive=True,
            )
            candidates = []
            for path, entry in sorted(entries.items()):
                if path.rsplit("/", 1)[-1] != "SKILL.md" or entry["type"] != "blob":
                    continue
                parent = path.rpartition("/")[0]
                subtree = entries[parent]["sha"] if parent else tree
                selected = [
                    item
                    for name, item in entries.items()
                    if not parent or name.startswith(parent + "/")
                ]
                candidate_path = "/".join(
                    part for part in (body.prefix, parent) if part
                )
                candidates.append(
                    {
                        "id": digest({"path": candidate_path, "tree_sha": subtree}),
                        "path": candidate_path,
                        "tree_sha": subtree,
                        "relative_path": parent,
                        "file_count": sum(item["type"] == "blob" for item in selected),
                        "entry_count": len(selected),
                        "total_bytes": sum(item.get("size", 0) for item in selected),
                    }
                )
            if len(candidates) > MAX_CANDIDATES:
                raise DiscoveryError("candidate_limit_exceeded")
            again = reader.get(prefix, "repository_identity", TREE_BYTES)
            if (
                type(again.get("id")) is not int
                or again.get("id") != repository["id"]
                or again.get("full_name") != repository["full_name"]
                or again.get("private") is not False
                or again.get("visibility") != "public"
            ):
                raise DiscoveryError("repository_identity_changed")
            with self.store.connect() as c:
                current = self._load(c, identifier, lock=True)
                current.update(
                    status="ready",
                    operation=None,
                    entries=entries,
                    candidates=candidates,
                    source={
                        "repository_id": repository["id"],
                        "repository": repository["full_name"],
                        "commit": commit_sha,
                        "root_tree": root,
                        "prefix_tree": tree,
                        "resolved_at": now(),
                        "api_version": API_VERSION,
                        "ancestors": ancestors,
                    },
                )
                self._save(c, current)
                return self.public(current)
        except Exception as error:  # noqa: BLE001 -- retain bounded intent even for unexpected validation errors
            self._failure(identifier, error)

    def _cache(self, identifier, sha, data=None):
        folder = self.root / identifier
        _no_links(folder)
        folder.mkdir(mode=0o700, exist_ok=True)
        if stat.S_IMODE(folder.stat().st_mode) != 0o700:
            raise DiscoveryError("private_discovery_storage_required")
        target = folder / oid(sha)
        _no_links(target)
        if data is not None and not target.exists():
            descriptor = os.open(
                target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size > MAX_SKILL_FILE_BYTES
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise DiscoveryError("cached_blob_integrity_failed")
            raw = source.read(MAX_SKILL_FILE_BYTES + 1)
        if len(raw) > MAX_SKILL_FILE_BYTES or git_hash("blob", raw) != sha:
            raise DiscoveryError("cached_blob_integrity_failed")
        return raw

    def _files(self, value, candidate):
        files, directories, manifest = {}, [], []
        base = candidate["relative_path"]
        for path, entry in value["entries"].items():
            if base and not path.startswith(base + "/"):
                continue
            relative = path[len(base) + 1 :] if base else path
            if entry["type"] == "tree":
                directories.append(relative)
                continue
            raw = self._cache(value["id"], entry["sha"])
            if len(raw) != entry["size"]:
                raise DiscoveryError("cached_blob_size_changed")
            mode = 0o755 if entry["mode"] == "100755" else 0o644
            files[relative] = FileContent(raw, mode)
            manifest.append(
                {
                    "path": relative,
                    "mode": mode,
                    "size": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "blob_sha": entry["sha"],
                }
            )
        return (
            files,
            tuple(sorted(directories)),
            sorted(manifest, key=lambda item: item["path"]),
        )

    def preview(self, identifier, body):
        value = self._claim(identifier, body, "preview")
        try:
            candidate = next(
                item for item in value["candidates"] if item["id"] == body.candidate_id
            )
            if (
                candidate["entry_count"] > MAX_SKILL_ENTRIES
                or candidate["total_bytes"] > MAX_SKILL_TOTAL_BYTES
            ):
                raise DiscoveryError("candidate_content_limit")
            reader = GitHubReader(
                self,
                value["id"],
                value["operation"]["id"],
                deadline_at=value["operation"]["deadline_at"],
                transport=self.transport,
            )
            prefix = "/repos/" + "/".join(
                quote(part, safe="") for part in value["input"]["repository"].split("/")
            )
            base = candidate["relative_path"]
            for path, entry in value["entries"].items():
                if entry["type"] != "blob" or base and not path.startswith(base + "/"):
                    continue
                if (
                    entry["size"] > MAX_SKILL_FILE_BYTES
                    or (path[len(base) + 1 :] if base else path).count("/")
                    > MAX_SKILL_DEPTH
                ):
                    raise DiscoveryError("candidate_content_limit")
                response = reader.get(
                    prefix + "/git/blobs/" + entry["sha"], "blob", BLOB_BYTES
                )
                if (
                    response.get("sha") != entry["sha"]
                    or type(response.get("size")) is not int
                    or response.get("size") != entry["size"]
                    or response.get("encoding") != "base64"
                    or not isinstance(response.get("content"), str)
                ):
                    raise DiscoveryError("blob_identity_mismatch")
                try:
                    encoded = response["content"].replace("\n", "")
                    raw = base64.b64decode(encoded, validate=True)
                except ValueError as error:
                    raise DiscoveryError("invalid_blob_encoding") from error
                if len(raw) != entry["size"] or git_hash("blob", raw) != entry["sha"]:
                    raise DiscoveryError("git_blob_hash_mismatch")
                self._cache(value["id"], entry["sha"], raw)
            files, directories, manifest = self._files(value, candidate)
            again = reader.get(prefix, "repository_identity", TREE_BYTES)
            if (
                type(again.get("id")) is not int
                or again.get("id") != value["source"]["repository_id"]
                or again.get("full_name") != value["source"]["repository"]
                or again.get("private") is not False
                or again.get("visibility") != "public"
            ):
                raise DiscoveryError("repository_identity_changed")
            prepared = prepare_bytes(files, directories, {})
            license_receipt = check_license(files)
            result = {
                "candidate_id": body.candidate_id,
                "instructions": files["SKILL.md"].data.decode("utf-8"),
                "files": manifest,
                "directories": list(directories),
                "license": license_receipt,
                "importable": license_receipt["status"] == "supported",
            }
            result["sha256"] = digest(
                {
                    "source": value["source"],
                    "candidate": candidate,
                    "preview": result,
                    "content_digest": prepared.content_digest,
                }
            )
            with self.store.connect() as c:
                current = self._load(c, identifier, lock=True)
                if current["operation"] != value["operation"]:
                    raise DiscoveryError("operation_claim_changed", 409)
                current["previews"][body.candidate_id] = result
                current.update(preview=result, status="preview_ready", operation=None)
                self._save(c, current)
                return self.public(current)
        except Exception as error:  # noqa: BLE001
            self._failure(value["id"], error)

    def import_skill(self, identifier, body):
        intent = digest(body.model_dump(exclude={"expected_revision"}))
        with self.store.connect() as c:
            old = self._load(c, identifier)
        if old["status"] == "imported":
            if old.get("import_intent") != intent:
                raise DiscoveryError("discovery_already_consumed", 409, identifier)
            saved = self.library.get("skill", old["imported"]["version_id"])
            skill = self.library.skill(saved)
            if not skill["integrity"]["valid"]:
                raise DiscoveryError("bound_skill_integrity_failed", 409, identifier)
            return {"discovery": self.public(old), "skill": skill}
        value = self._claim(identifier, body, "import")
        try:
            preview = value["previews"].get(body.candidate_id)
            if not preview or preview["sha256"] != body.preview_sha256:
                raise DiscoveryError("preview_digest_mismatch", 409)
            if not preview["importable"]:
                raise DiscoveryError("license_preview_only")
            candidate = next(
                item for item in value["candidates"] if item["id"] == body.candidate_id
            )
            files, directories, manifest = self._files(value, candidate)
            if (
                manifest != preview["files"]
                or check_license(files) != preview["license"]
            ):
                raise DiscoveryError("preview_content_changed", 409)
            repository = value["source"]["repository"]
            source = (
                "https://github.com/"
                + repository
                + "/tree/"
                + value["source"]["commit"]
            )
            if candidate["path"]:
                source += "/" + quote(candidate["path"], safe="/")
            prepared = prepare_bytes(
                files,
                directories,
                {
                    "source": source,
                    "commit": value["source"]["commit"],
                    "license_status": "declared",
                    "license_text": "\n\n".join(
                        item["text"] for item in preview["license"]["files"]
                    ),
                    "scope": body.scope,
                    "permissions": body.permissions,
                },
            )
            record = self.library.skills.persist(prepared)
            pending = {**record, "review_status": "pending", "review_history": []}
            try:
                anchor = self.library.get("skill", record["version_id"])
            except KeyError:
                anchor = pending
            validated = self.library.skill(anchor)
            if not validated["integrity"]["valid"]:
                raise DiscoveryError("bound_skill_integrity_failed", 409)
            immutable = {
                key: value
                for key, value in anchor.items()
                if key not in {"review_status", "review_history"}
            }
            # Filesystem publication precedes this transaction; an orphan is not selectable.
            with self.store.connect() as c:
                current = self._load(c, identifier, lock=True)
                if current["operation"] != value["operation"]:
                    raise DiscoveryError("operation_claim_changed", 409)
                saved = self.library.bind(
                    "skill",
                    record["version_id"],
                    pending,
                    c,
                )
                if {
                    key: value
                    for key, value in saved.items()
                    if key not in {"review_status", "review_history"}
                } != immutable:
                    raise DiscoveryError("bound_skill_integrity_failed", 409)
                skill = {**saved, "integrity": validated["integrity"]}
                current.update(
                    status="imported",
                    operation=None,
                    import_intent=intent,
                    imported={
                        "candidate_id": body.candidate_id,
                        "version_id": saved["version_id"],
                    },
                )
                self._save(c, current)
                return {"discovery": self.public(current), "skill": skill}
        except Exception as error:  # noqa: BLE001
            self._failure(value["id"], error)


def install_discovery_routes(app, settings, store, auth):
    service = Discovery(settings, store)

    def invoke(function, *args):
        try:
            return function(*args)
        except DiscoveryError as error:
            detail = {"code": error.code}
            if error.discovery_id:
                detail["discovery_id"] = error.discovery_id
            raise HTTPException(error.status, detail) from None

    @app.get("/api/skills/discoveries", dependencies=auth)
    def discoveries():
        return {"discoveries": invoke(service.list)}

    @app.post("/api/skills/discoveries", dependencies=auth)
    def discover(body: DiscoveryInput):
        return invoke(service.create, body)

    @app.get("/api/skills/discoveries/{identifier}", dependencies=auth)
    def get_discovery(identifier: str):
        return invoke(service.get, identifier)

    @app.post("/api/skills/discoveries/{identifier}/preview", dependencies=auth)
    def preview(identifier: str, body: PreviewInput):
        return invoke(service.preview, identifier, body)

    @app.post("/api/skills/discoveries/{identifier}/import", dependencies=auth)
    def import_skill(identifier: str, body: ImportInput):
        return invoke(service.import_skill, identifier, body)


def check_license(files):
    """Fail closed on incomplete license text and ambiguous candidate boundaries."""
    found, reasons, supported = [], [], []
    for path, content in files.items():
        name = path.rsplit("/", 1)[-1].casefold()
        if path != "SKILL.md" and name == "skill.md":
            reasons.append("nested_skill_boundary")
        if name.startswith(("license", "licence", "copying", "notice")):
            try:
                text = content.data.decode("utf-8")
            except UnicodeDecodeError:
                reasons.append("license_not_utf8")
                continue
            normalized = " ".join(text.split())
            kind = None
            if (
                "/" not in path
                and hashlib.sha256(normalized.encode()).hexdigest()
                == APACHE_TEXT_SHA256
            ):
                kind = "Apache-2.0"
            elif "/" not in path and complete_mit(text):
                kind = "MIT"
            if kind:
                supported.append(kind)
            else:
                reasons.append("unknown_or_conflicting_license_text")
            found.append(
                {
                    "path": path,
                    "text": text,
                    "sha256": hashlib.sha256(content.data).hexdigest(),
                    "blob_sha": git_hash("blob", content.data),
                }
            )
    if not found:
        reasons.append("candidate_license_missing")
    if len(found) != 1 or len(set(supported)) != 1:
        reasons.append("license_scope_ambiguous")
    expected = supported[0] if len(set(supported)) == 1 else None
    for path, content in files.items():
        # Declarations are data, never rendered Markdown or executed code. Only
        # UTF-8 files can carry a declaration we understand in this first slice.
        try:
            text = content.data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        lower = text.casefold()
        if any(
            term in lower
            for term in (
                "source-available",
                "all rights reserved",
                "non-commercial",
                "noncommercial",
                "business source license",
                "proprietary",
            )
        ):
            reasons.append("restrictive_file_declaration")
        declarations = re.findall(
            r"SPDX-License-Identifier\s*:\s*([^\r\n]+)", text, flags=re.IGNORECASE
        )
        for declaration in declarations:
            if declaration.strip(" \t*/#") != expected:
                reasons.append("conflicting_file_license")
        for declaration in re.findall(
            r"^\s*(?:[#/*>-]\s*)?licen[cs]e\s*:\s*(.+)$",
            text,
            flags=re.IGNORECASE | re.MULTILINE,
        ):
            declaration = declaration.strip(" \t\"'*/")
            references = {item["path"] for item in found}
            permitted = {
                expected,
                *references,
                *("Complete terms in " + item for item in references),
            }
            if declaration not in permitted:
                reasons.append("conflicting_file_license")
        # A known alternate license in an explicit license statement is enough
        # to make the boundary ambiguous. This is deliberately conservative.
        if path not in {item["path"] for item in found}:
            for line in text.splitlines():
                if not re.search(r"\blicen[cs](?:e|ed|ing)\b", line, re.IGNORECASE):
                    continue
                for pattern, kind in (
                    (r"\bMIT\b", "MIT"),
                    (r"\bApache(?:[- ](?:License[- ]?)?2(?:\.0)?)?\b", "Apache-2.0"),
                    (r"\b(?:[AL]?GPL|BSD|MPL|EPL|ISC|CC-BY|Unlicense)\b", "other"),
                ):
                    if re.search(pattern, line, re.IGNORECASE) and kind != expected:
                        reasons.append("conflicting_file_license")
    return {
        "status": "preview_only" if reasons else "supported",
        "spdx": supported[0] if not reasons else None,
        "files": sorted(found, key=lambda item: item["path"]),
        "reasons": sorted(set(reasons)),
    }


def complete_mit(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines and lines[0] == "MIT License":
        lines.pop(0)
    copyrights = 0
    while lines and re.fullmatch(
        r"Copyright (?:\(c\) |© )?\d{4}(?:[-, ]\d{4})* [^\r\n]{1,256}", lines[0]
    ):
        copyrights += 1
        lines.pop(0)
    return copyrights > 0 and " ".join(" ".join(lines).split()) == " ".join(
        MIT_BODY.split()
    )


MIT_BODY = """Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE."""


# Whitespace-normalized complete ASF License 2.0 text, including its appendix.
# Source: https://www.apache.org/licenses/LICENSE-2.0.txt
APACHE_TEXT_SHA256 = "0ffddef9e48f8a09aed5caf2d44f7ba1c1be2d9b8e0a6f693b1635b2d5566645"

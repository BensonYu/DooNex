"""Default-closed community publication, separate from private Library approval.

Only an explicit maintainer read/preview uses the bounded public GitHub reader.
Published responses and archives come from a dedicated immutable whitelist.
Tokens grant submission or self-report permission, never workbench authority.
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import io
import os
import re
import secrets
import stat
import tarfile
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from fastapi import Header, HTTPException, Response
from psycopg.types.json import Jsonb
from pydantic import Field, field_validator

from doonex.discovery import (
    Discovery,
    DiscoveryError,
    DiscoveryInput,
    canonical,
    check_license,
    digest,
)
from doonex.discovery import (
    PreviewInput as DiscoveryPreview,
)
from doonex.models import StrictModel
from doonex.skills import MAX_SKILL_TOTAL_BYTES, _no_links, prepare_bytes
from doonex.store import now

COMMUNITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS community_settings (singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), data jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS community_quotas (day date PRIMARY KEY, data jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS community_tokens (id uuid PRIMARY KEY, token_hash text UNIQUE NOT NULL, kind text NOT NULL CHECK(kind IN ('submission','usage')), data jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS community_submissions (id uuid PRIMARY KEY, token_id uuid NOT NULL REFERENCES community_tokens(id), request_id uuid NOT NULL, data jsonb NOT NULL, UNIQUE(token_id,request_id));
CREATE TABLE IF NOT EXISTS community_releases (id text PRIMARY KEY, state text NOT NULL CHECK(state IN ('published','withdrawn')), data jsonb NOT NULL, artifact jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS community_distributions (id uuid PRIMARY KEY, request_id uuid UNIQUE NOT NULL, release_id text NOT NULL REFERENCES community_releases(id), data jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS community_usage_reports (token_id uuid NOT NULL REFERENCES community_tokens(id), event_id uuid NOT NULL, data jsonb NOT NULL, PRIMARY KEY(token_id,event_id));
"""
LIMITS = {
    "tokens": {"daily": 20, "total": 200},
    "submissions": {"daily": 30, "total": 1000},
    "source_reads": {"daily": 20, "total": 1000},
    "previews": {"daily": 20, "total": 1000},
    "releases": {"daily": 20, "total": 1000},
    "distributions": {"daily": 100, "total": 10000},
    "downloads": {"daily": 200, "total": 20000},
    "self_reports": {"daily": 100, "total": 10000},
}
PUBLIC_FIELDS = (
    "title",
    "summary",
    "category",
    "author_label",
    "author_statement",
    "identity_status",
    "source",
    "content_digest",
    "files",
    "directories",
    "instructions",
    "license",
    "archive_sha256",
)


class CommunityError(ValueError):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code, self.status = code, status


def require(value, code, status=400):
    if not value:
        raise CommunityError(code, status)


def identifier(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise CommunityError("community_invalid_uuid") from None


def revision(value):
    return digest({k: v for k, v in value.items() if k != "revision"})


class CommunityModel(StrictModel):
    @field_validator("*", mode="after")
    @classmethod
    def bounded_text(cls, value):
        if isinstance(value, str) and value and not value.strip():
            raise ValueError("community_blank_text")
        if isinstance(value, str) and any(
            ord(c) < 32 and c not in "\n\t" or ord(c) == 127 for c in value
        ):
            raise ValueError("community_control_character")
        return value


class SettingsInput(CommunityModel):
    enabled: bool = Field(strict=True)
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class TokenInput(CommunityModel):
    label: str = Field(min_length=1, max_length=80)
    expires_at: datetime
    max_submissions: int = Field(default=3, ge=1, le=20)


class SubmissionInput(CommunityModel):
    request_id: UUID
    repository: str = Field(min_length=3, max_length=201)
    ref: str = Field(min_length=1, max_length=255)
    prefix: str = Field(default="", max_length=1024)
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(default="", max_length=1000)
    category: str = Field(min_length=1, max_length=80)
    author_label: str = Field(min_length=1, max_length=80)
    author_statement: str = Field(default="", max_length=2000)


class RevisionInput(CommunityModel):
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class ReadInput(RevisionInput):
    request_id: UUID


class PreviewInput(ReadInput):
    candidate_id: str = Field(pattern=r"^[a-f0-9]{64}$")


class ReviewInput(RevisionInput):
    preview_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["approved", "rejected"]
    reviewer_label: str = Field(min_length=1, max_length=80)
    note: str = Field(default="", max_length=1000)


class PublishInput(RevisionInput):
    preview_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirm_public_source: Literal[True]
    confirm_license: Literal[True]
    confirm_publish: Literal[True]

    @field_validator(
        "confirm_public_source", "confirm_license", "confirm_publish", mode="before"
    )
    @classmethod
    def explicit(cls, value):
        if value is not True:
            raise ValueError("community_explicit_confirmation_required")
        return value


class WithdrawInput(CommunityModel):
    reason: str = Field(min_length=1, max_length=1000)


class DistributionInput(CommunityModel):
    request_id: UUID


class UsageInput(CommunityModel):
    event_id: UUID
    outcome: Literal["installed", "used", "failed"]


def archive_bytes(files, directories):
    output = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=output, mode="wb", mtime=0, filename="") as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        for name in directories:
            member = tarfile.TarInfo(name + "/")
            member.type, member.mode = tarfile.DIRTYPE, 0o700
            archive.addfile(member)
        for name, content in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.mode, member.size = content.mode, len(content.data)
            archive.addfile(member, io.BytesIO(content.data))
    return output.getvalue()


def private_read(path, maximum=MAX_SKILL_TOTAL_BYTES + 1024 * 1024):
    _no_links(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        require(
            stat.S_ISREG(info.st_mode)
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1,
            "community_artifact_not_private",
        )
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            raw = source.read(maximum + 1)
        require(
            len(raw) <= maximum and len(raw) == info.st_size, "community_artifact_size"
        )
        return raw
    finally:
        os.close(descriptor)


class Community:
    def __init__(self, settings, store, *, transport=None):
        self.settings, self.store = settings, store
        self.discovery = Discovery(settings, store, transport=transport)
        self.root = Path(settings.home).absolute() / "community"
        _no_links(self.root)
        self.root.mkdir(mode=0o700, exist_ok=True)
        require(
            stat.S_IMODE(self.root.stat().st_mode) == 0o700,
            "community_storage_not_private",
        )

    @staticmethod
    def defaults():
        value = {
            "enabled": False,
            "title": "DooNex Skill community",
            "description": "",
            "limits": LIMITS,
            "totals": {k: 0 for k in LIMITS},
            "commercial_enabled": False,
        }
        value["revision"] = revision(value)
        return value

    def _settings(self, c, *, lock=False):
        if lock:
            c.execute(
                "INSERT INTO community_settings VALUES(true,%s) ON CONFLICT DO NOTHING",
                (Jsonb(self.defaults()),),
            )
        row = c.execute(
            "SELECT data FROM community_settings WHERE singleton"
            + (" FOR UPDATE" if lock else "")
        ).fetchone()
        value = row["data"] if row else self.defaults()
        require(value["revision"] == revision(value), "community_settings_changed")
        return value

    def _save_settings(self, c, value):
        value["revision"] = revision(value)
        c.execute(
            "UPDATE community_settings SET data=%s WHERE singleton", (Jsonb(value),)
        )

    def settings_get(self):
        with self.store.connect() as c:
            return self._settings(c)

    def configure(self, body):
        with self.store.connect() as c:
            value = self._settings(c, lock=True)
            require(
                value["revision"] == body.expected_revision,
                "community_revision_conflict",
                409,
            )
            value.update(
                enabled=body.enabled, title=body.title, description=body.description
            )
            self._save_settings(c, value)
        return self.settings_get()

    def _consume(self, c, kind, *, enabled=True):
        value = self._settings(c, lock=True)
        require(not enabled or value["enabled"], "community_disabled", 404)
        day = datetime.now(UTC).date()
        initial = {"limits": value["limits"], "counts": {k: 0 for k in LIMITS}}
        c.execute(
            "INSERT INTO community_quotas VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (day, Jsonb(initial)),
        )
        daily = c.execute(
            "SELECT data FROM community_quotas WHERE day=%s FOR UPDATE", (day,)
        ).fetchone()["data"]
        require(
            daily["counts"][kind] < daily["limits"][kind]["daily"]
            and value["totals"][kind] < value["limits"][kind]["total"],
            "community_capacity_exhausted",
            429,
        )
        daily["counts"][kind] += 1
        value["totals"][kind] += 1
        c.execute(
            "UPDATE community_quotas SET data=%s WHERE day=%s", (Jsonb(daily), day)
        )
        self._save_settings(c, value)

    def _auth(self, c, authorization, kind):
        require(
            isinstance(authorization, str) and authorization.startswith("Bearer "),
            "community_token_required",
            401,
        )
        token = authorization[7:]
        require(40 <= len(token) <= 200, "community_token_invalid", 401)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        row = c.execute(
            "SELECT id,token_hash,kind,data FROM community_tokens WHERE token_hash=%s FOR UPDATE",
            (token_hash,),
        ).fetchone()
        require(
            row
            and row["kind"] == kind
            and hmac.compare_digest(row["token_hash"], token_hash),
            "community_token_invalid",
            401,
        )
        value = row["data"]
        require(
            not value["revoked_at"]
            and datetime.fromisoformat(value["expires_at"]) > datetime.now(UTC),
            "community_token_inactive",
            401,
        )
        return str(row["id"]), value

    def _mint(self, c, kind, label, expires_at, **extra):
        token, token_id = "dxc_" + secrets.token_urlsafe(32), str(uuid4())
        value = {
            "id": token_id,
            "label": label,
            "expires_at": expires_at,
            "revoked_at": None,
            "created_at": now(),
            **extra,
        }
        c.execute(
            "INSERT INTO community_tokens VALUES(%s,%s,%s,%s)",
            (token_id, hashlib.sha256(token.encode()).hexdigest(), kind, Jsonb(value)),
        )
        return value, token

    def issue_token(self, body):
        require(
            body.expires_at.tzinfo is not None
            and datetime.now(UTC)
            < body.expires_at
            <= datetime.now(UTC) + timedelta(days=30),
            "community_token_expiry_bound",
        )
        with self.store.connect() as c:
            self._consume(c, "tokens")
            value, token = self._mint(
                c,
                "submission",
                body.label,
                body.expires_at.astimezone(UTC).isoformat(),
                max_submissions=body.max_submissions,
                submissions_used=0,
            )
        return {"credential": value, "token": token, "shown_once": True}

    def tokens(self):
        with self.store.connect() as c:
            return [
                {"kind": row["kind"], **row["data"]}
                for row in c.execute(
                    "SELECT kind,data FROM community_tokens ORDER BY id"
                )
            ]

    def revoke(self, token_id):
        with self.store.connect() as c:
            self._settings(c, lock=True)
            row = c.execute(
                "SELECT data FROM community_tokens WHERE id=%s FOR UPDATE",
                (identifier(token_id),),
            ).fetchone()
            require(row is not None, "community_token_not_found", 404)
            value = row["data"]
            value["revoked_at"] = value["revoked_at"] or now()
            c.execute(
                "UPDATE community_tokens SET data=%s WHERE id=%s",
                (Jsonb(value), token_id),
            )
        return value

    def _submission(self, c, submission_id, *, lock=False):
        row = c.execute(
            "SELECT token_id,data FROM community_submissions WHERE id=%s"
            + (" FOR UPDATE" if lock else ""),
            (identifier(submission_id),),
        ).fetchone()
        require(row is not None, "community_submission_not_found", 404)
        value = row["data"]
        require(value["revision"] == revision(value), "community_submission_changed")
        return str(row["token_id"]), value

    def _save(self, c, value):
        value["updated_at"], value["revision"] = now(), None
        value["revision"] = revision(value)
        c.execute(
            "UPDATE community_submissions SET data=%s WHERE id=%s",
            (Jsonb(value), value["id"]),
        )

    def submit(self, authorization, body):
        source = DiscoveryInput(
            repository=body.repository, ref=body.ref, prefix=body.prefix
        ).model_dump()
        submitted = {**body.model_dump(mode="json"), "source": source}
        with self.store.connect() as c:
            self._settings(c, lock=True)
            token_id, token = self._auth(c, authorization, "submission")
            old = c.execute(
                "SELECT data FROM community_submissions WHERE token_id=%s AND request_id=%s",
                (token_id, body.request_id),
            ).fetchone()
            if old:
                require(
                    old["data"]["input"] == submitted,
                    "community_submission_intent_conflict",
                    409,
                )
                return self.own_projection(old["data"])
            self._consume(c, "submissions")
            require(
                token["submissions_used"] < token["max_submissions"],
                "community_token_capacity_exhausted",
                429,
            )
            token["submissions_used"] += 1
            c.execute(
                "UPDATE community_tokens SET data=%s WHERE id=%s",
                (Jsonb(token), token_id),
            )
            value = {
                "id": str(uuid4()),
                "input": submitted,
                "state": "submitted",
                "operation": None,
                "discovery_id": None,
                "discovery": None,
                "preview": None,
                "review": None,
                "publish_intent": None,
                "release_id": None,
                "error": None,
                "created_at": now(),
                "updated_at": now(),
            }
            value["revision"] = revision(value)
            c.execute(
                "INSERT INTO community_submissions VALUES(%s,%s,%s,%s)",
                (value["id"], token_id, body.request_id, Jsonb(value)),
            )
        return self.own_projection(value)

    @staticmethod
    def own_projection(value):
        return {
            k: value[k]
            for k in (
                "id",
                "input",
                "state",
                "release_id",
                "error",
                "created_at",
                "updated_at",
            )
        }

    def submissions(self, authorization=None):
        with self.store.connect() as c:
            if authorization is not None:
                token_id, _token = self._auth(c, authorization, "submission")
                rows = c.execute(
                    "SELECT data FROM community_submissions WHERE token_id=%s ORDER BY id",
                    (token_id,),
                ).fetchall()
                return [self.own_projection(row["data"]) for row in rows]
            return [
                row["data"]
                for row in c.execute(
                    "SELECT data FROM community_submissions ORDER BY data->>'created_at' DESC,id"
                )
            ]

    def get_submission(self, submission_id, authorization=None):
        with self.store.connect() as c:
            owner, value = self._submission(c, submission_id)
            if authorization is not None:
                token_id, _token = self._auth(c, authorization, "submission")
                require(owner == token_id, "community_submission_not_found", 404)
                return self.own_projection(value)
            return value

    def _claim(self, submission_id, body, kind, allowed):
        with self.store.connect() as c:
            self._settings(c, lock=True)
            _owner, value = self._submission(c, submission_id, lock=True)
            require(
                value["revision"] == body.expected_revision,
                "community_revision_conflict",
                409,
            )
            require(
                value["state"] in allowed and value["operation"] is None,
                "community_operation_not_replayable",
                409,
            )
            self._consume(c, kind)
            value.update(
                operation={
                    "kind": kind,
                    "request_id": str(body.request_id),
                    "at": now(),
                },
                state=kind + "_in_progress",
            )
            self._save(c, value)
        return value

    def _failed(self, submission_id, error):
        with self.store.connect() as c:
            _owner, value = self._submission(c, submission_id, lock=True)
            value.update(
                state="source_unknown",
                error=error.code
                if isinstance(error, (CommunityError, DiscoveryError))
                else "community_source_operation_failed",
            )
            if isinstance(error, DiscoveryError) and error.discovery_id:
                value["discovery_id"] = error.discovery_id
            self._save(c, value)
        raise CommunityError(value["error"], 409) from None

    def read(self, submission_id, body):
        value = self._claim(submission_id, body, "source_reads", {"submitted"})
        try:
            receipt = self.discovery.create(DiscoveryInput(**value["input"]["source"]))
            with self.store.connect() as c:
                _owner, current = self._submission(c, submission_id, lock=True)
                require(
                    current["operation"] == value["operation"],
                    "community_operation_changed",
                    409,
                )
                current.update(
                    discovery_id=receipt["id"],
                    discovery=receipt,
                    operation=None,
                    state="source_ready",
                )
                self._save(c, current)
            return self.get_submission(submission_id)
        except Exception as error:  # noqa: BLE001 -- retain unknown admission, never replay
            self._failed(submission_id, error)

    def preview(self, submission_id, body):
        value = self._claim(submission_id, body, "previews", {"source_ready"})
        try:
            receipt = self.discovery.preview(
                value["discovery_id"],
                DiscoveryPreview(
                    candidate_id=body.candidate_id,
                    expected_revision=value["discovery"]["revision"],
                ),
            )
            with self.store.connect() as c:
                raw = self.discovery._load(c, receipt["id"])
            candidate = next(
                item for item in raw["candidates"] if item["id"] == body.candidate_id
            )
            files, directories, file_manifest = self.discovery._files(raw, candidate)
            license_receipt = check_license(files)
            prepared = prepare_bytes(files, directories, {})
            require(
                receipt["preview"]["files"] == file_manifest,
                "community_content_invalid",
            )
            archive = archive_bytes(files, directories)
            public = {
                k: value["input"][k]
                for k in (
                    "title",
                    "summary",
                    "category",
                    "author_label",
                    "author_statement",
                )
            }
            public.update(
                identity_status="self_reported",
                source={
                    "repository": raw["source"]["repository"],
                    "commit": raw["source"]["commit"],
                    "path": candidate["path"],
                    "tree_sha": candidate["tree_sha"],
                },
                content_digest=prepared.content_digest,
                files=file_manifest,
                directories=list(directories),
                instructions=files["SKILL.md"].data.decode("utf-8"),
                license=license_receipt,
                archive_sha256=hashlib.sha256(archive).hexdigest(),
            )
            report = canonical(public)
            preview_hash = hashlib.sha256(report).hexdigest()
            destination = self.root / preview_hash
            if destination.exists():
                require(
                    private_read(destination / "report.json") == report
                    and private_read(destination / "candidate.tar.gz") == archive,
                    "community_existing_snapshot_changed",
                )
            else:
                with tempfile.TemporaryDirectory(
                    prefix=".preview-", dir=self.root
                ) as temporary:
                    staging = Path(temporary) / "snapshot"
                    staging.mkdir(mode=0o700)
                    for name, data in (
                        ("report.json", report),
                        ("candidate.tar.gz", archive),
                    ):
                        with (staging / name).open("xb") as output:
                            os.fchmod(output.fileno(), 0o600)
                            output.write(data)
                            output.flush()
                            os.fsync(output.fileno())
                    staging.rename(destination)
            preview = {
                "sha256": preview_hash,
                "public": public,
                "discovery_preview_sha256": receipt["preview"]["sha256"],
            }
            with self.store.connect() as c:
                _owner, current = self._submission(c, submission_id, lock=True)
                require(
                    current["operation"] == value["operation"],
                    "community_operation_changed",
                    409,
                )
                current.update(
                    state="preview_ready"
                    if license_receipt["status"] == "supported"
                    else "preview_only",
                    operation=None,
                    discovery=receipt,
                    preview=preview,
                )
                self._save(c, current)
            return self.get_submission(submission_id)
        except Exception as error:  # noqa: BLE001
            self._failed(submission_id, error)

    def _snapshot(self, value):
        preview = value["preview"]
        require(
            preview and digest(preview["public"]) == preview["sha256"],
            "community_preview_changed",
        )
        root = self.root / preview["sha256"]
        _no_links(root)
        require(
            stat.S_IMODE(root.stat().st_mode) == 0o700
            and sorted(p.name for p in root.iterdir())
            == ["candidate.tar.gz", "report.json"],
            "community_snapshot_changed",
        )
        report, archive = (
            private_read(root / "report.json"),
            private_read(root / "candidate.tar.gz"),
        )
        require(
            report == canonical(preview["public"])
            and hashlib.sha256(archive).hexdigest()
            == preview["public"]["archive_sha256"],
            "community_snapshot_changed",
        )
        return archive

    def review(self, submission_id, body):
        old = self.get_submission(submission_id)
        self._snapshot(old)
        with self.store.connect() as c:
            require(self._settings(c, lock=True)["enabled"], "community_disabled", 404)
            _owner, value = self._submission(c, submission_id, lock=True)
            require(
                value["revision"] == body.expected_revision
                and value["state"] == "preview_ready"
                and value["preview"]["sha256"] == body.preview_sha256,
                "community_review_conflict",
                409,
            )
            value.update(
                state=body.status,
                review={
                    "status": body.status,
                    "reviewer_label": body.reviewer_label,
                    "note": body.note,
                    "preview_sha256": body.preview_sha256,
                    "at": now(),
                },
            )
            self._save(c, value)
        return self.get_submission(submission_id)

    def publish(self, submission_id, body):
        old = self.get_submission(submission_id)
        intent = body.model_dump()
        if old["release_id"]:
            require(
                old["publish_intent"] == intent,
                "community_publish_intent_conflict",
                409,
            )
            return self.release(old["release_id"], admin=True)
        self._snapshot(old)
        with self.store.connect() as c:
            self._settings(c, lock=True)
            _owner, value = self._submission(c, submission_id, lock=True)
            if value["release_id"]:
                require(
                    value["publish_intent"] == intent,
                    "community_publish_intent_conflict",
                    409,
                )
                release_id = value["release_id"]
            else:
                require(
                    value["revision"] == body.expected_revision
                    and value["state"] == "approved"
                    and value["review"]["preview_sha256"]
                    == body.preview_sha256
                    == value["preview"]["sha256"],
                    "community_publish_not_approved",
                    409,
                )
                self._consume(c, "releases")
                public = {k: value["preview"]["public"][k] for k in PUBLIC_FIELDS}
                release_id = digest(public)
                published = {
                    "id": release_id,
                    **public,
                    "published_at": now(),
                    "review": {
                        "reviewer_label": value["review"]["reviewer_label"],
                        "approved_at": value["review"]["at"],
                    },
                    "quality": {"controller_verified": False, "evidence_count": 0},
                }
                artifact = {
                    "preview_sha256": value["preview"]["sha256"],
                    "archive_sha256": public["archive_sha256"],
                }
                c.execute(
                    "INSERT INTO community_releases VALUES(%s,'published',%s,%s) ON CONFLICT DO NOTHING",
                    (release_id, Jsonb(published), Jsonb(artifact)),
                )
                existing = c.execute(
                    "SELECT state,data,artifact FROM community_releases WHERE id=%s",
                    (release_id,),
                ).fetchone()
                require(
                    existing["state"] == "published"
                    and existing["artifact"] == artifact
                    and all(existing["data"][k] == public[k] for k in PUBLIC_FIELDS),
                    "community_release_conflict",
                    409,
                )
                value.update(
                    state="published", release_id=release_id, publish_intent=intent
                )
                self._save(c, value)
        return self.release(release_id, admin=True)

    def release(self, release_id, *, admin=False, connection=None):
        if connection is None:
            with self.store.connect() as c:
                return self.release(release_id, admin=admin, connection=c)
        if not admin:
            require(self._settings(connection)["enabled"], "community_disabled", 404)
        row = connection.execute(
            "SELECT state,data FROM community_releases WHERE id=%s", (release_id,)
        ).fetchone()
        require(
            row and (admin or row["state"] == "published"),
            "community_release_not_found",
            404,
        )
        return self.release_projection(row["state"], row["data"])

    @staticmethod
    def release_projection(state, value):
        public = {key: value[key] for key in PUBLIC_FIELDS}
        require(digest(public) == value["id"], "community_release_content_changed")
        return {
            "state": state,
            "id": value["id"],
            **public,
            "published_at": value["published_at"],
            "review": {
                key: value["review"][key] for key in ("reviewer_label", "approved_at")
            },
            "quality": {"controller_verified": False, "evidence_count": 0},
        }

    def releases(self, *, admin=False):
        with self.store.connect() as c:
            if not admin:
                require(self._settings(c)["enabled"], "community_disabled", 404)
            rows = c.execute(
                "SELECT state,data FROM community_releases"
                + ("" if admin else " WHERE state='published'")
                + " ORDER BY id"
            ).fetchall()
        return [self.release_projection(row["state"], row["data"]) for row in rows]

    def withdraw(self, release_id, body):
        with self.store.connect() as c:
            self._settings(c, lock=True)
            row = c.execute(
                "SELECT state,artifact FROM community_releases WHERE id=%s FOR UPDATE",
                (release_id,),
            ).fetchone()
            require(row is not None, "community_release_not_found", 404)
            if row["state"] != "withdrawn":
                artifact = {
                    **row["artifact"],
                    "withdrawn_at": now(),
                    "withdraw_reason": body.reason,
                }
                c.execute(
                    "UPDATE community_releases SET state='withdrawn',artifact=%s WHERE id=%s",
                    (Jsonb(artifact), release_id),
                )
        return self.release(release_id, admin=True)

    def distribute(self, release_id, body):
        with self.store.connect() as c:
            self._settings(c, lock=True)
            release = self.release(release_id, connection=c)
            old = c.execute(
                "SELECT data FROM community_distributions WHERE request_id=%s",
                (body.request_id,),
            ).fetchone()
            if old:
                require(
                    old["data"]["release_id"] == release_id,
                    "community_distribution_intent_conflict",
                    409,
                )
                return {
                    "receipt": old["data"]["receipt"],
                    "usage_token": None,
                    "shown_once": True,
                }
            self._consume(c, "distributions")
            receipt = {
                "id": str(uuid4()),
                "release_id": release_id,
                "source": "distribution_request",
                "created_at": now(),
                "installation_verified": False,
                "content_digest": release["content_digest"],
                "archive_sha256": release["archive_sha256"],
            }
            token, raw = self._mint(
                c,
                "usage",
                "distribution self-report",
                (datetime.now(UTC) + timedelta(days=7)).isoformat(),
                distribution_id=receipt["id"],
                release_id=release_id,
                reports_used=0,
                max_reports=3,
            )
            value = {
                "release_id": release_id,
                "receipt": receipt,
                "usage_token_id": token["id"],
                "downloads": 0,
                "download_admissions": [],
            }
            c.execute(
                "INSERT INTO community_distributions VALUES(%s,%s,%s,%s)",
                (receipt["id"], body.request_id, release_id, Jsonb(value)),
            )
        return {"receipt": receipt, "usage_token": raw, "shown_once": True}

    def download(self, distribution_id):
        with self.store.connect() as c:
            self._settings(c, lock=True)
            row = c.execute(
                "SELECT release_id,data FROM community_distributions WHERE id=%s FOR UPDATE",
                (identifier(distribution_id),),
            ).fetchone()
            require(row is not None, "community_distribution_not_found", 404)
            release = self.release(row["release_id"], connection=c)
            bound = c.execute(
                "SELECT artifact FROM community_releases WHERE id=%s",
                (row["release_id"],),
            ).fetchone()["artifact"]
            require(
                isinstance(bound["preview_sha256"], str)
                and re.fullmatch(r"[a-f0-9]{64}", bound["preview_sha256"]),
                "community_snapshot_identifier_invalid",
            )
            # Read and hash the bounded immutable bytes before admitting this
            # response. A later withdrawal cannot retract an admitted response.
            raw = private_read(self.root / bound["preview_sha256"] / "candidate.tar.gz")
            require(
                hashlib.sha256(raw).hexdigest()
                == bound["archive_sha256"]
                == release["archive_sha256"],
                "community_download_integrity_failed",
            )
            require(
                row["data"]["downloads"] < 3,
                "community_distribution_download_limit",
                429,
            )
            self._consume(c, "downloads")
            value = row["data"]
            value["downloads"] += 1
            value["download_admissions"].append(
                {"at": now(), "status": "response_admitted_transport_outcome_unknown"}
            )
            c.execute(
                "UPDATE community_distributions SET data=%s WHERE id=%s",
                (Jsonb(value), distribution_id),
            )
        return raw

    def self_report(self, authorization, body):
        with self.store.connect() as c:
            self._settings(c, lock=True)
            token_id, token = self._auth(c, authorization, "usage")
            old = c.execute(
                "SELECT data FROM community_usage_reports WHERE token_id=%s AND event_id=%s",
                (token_id, body.event_id),
            ).fetchone()
            event = {
                "event_id": str(body.event_id),
                "release_id": token["release_id"],
                "distribution_id": token["distribution_id"],
                "outcome": body.outcome,
                "source": "self_reported",
                "controller_verified": False,
            }
            if old:
                require(
                    all(old["data"][k] == v for k, v in event.items()),
                    "community_usage_intent_conflict",
                    409,
                )
                return old["data"]
            self._consume(c, "self_reports")
            require(
                token["reports_used"] < token["max_reports"],
                "community_usage_token_capacity",
                429,
            )
            event["reported_at"] = now()
            c.execute(
                "INSERT INTO community_usage_reports VALUES(%s,%s,%s)",
                (token_id, body.event_id, Jsonb(event)),
            )
            token["reports_used"] += 1
            c.execute(
                "UPDATE community_tokens SET data=%s WHERE id=%s",
                (Jsonb(token), token_id),
            )
        return event

    def public_site(self):
        value = self.settings_get()
        require(value["enabled"], "community_disabled", 404)
        return {
            "title": value["title"],
            "description": value["description"],
            "commercial": {"enabled": False},
            "identity_policy": "Author labels and usage reports are self-reported",
            "verified_leaderboard": [],
            "leaderboard_reason": "no_controller_verified_evidence",
        }

    def release_binding(self, release_id, *, include_withdrawn=False):
        """Private integration contract for independently verified evidence.

        This proves publication identity and bytes only. Callers must separately
        verify actual Run/Skill/event evidence; it never promotes a self-report.
        """
        with self.store.connect() as c:
            release = self.release(release_id, admin=include_withdrawn, connection=c)
            artifact = c.execute(
                "SELECT artifact FROM community_releases WHERE id=%s", (release_id,)
            ).fetchone()["artifact"]
        preview = {
            "sha256": artifact["preview_sha256"],
            "public": {key: release[key] for key in PUBLIC_FIELDS},
        }
        self._snapshot({"preview": preview})
        return {
            "release_id": release_id,
            "state": release["state"],
            **{
                key: release[key]
                for key in (
                    "content_digest",
                    "source",
                    "files",
                    "directories",
                    "archive_sha256",
                    "published_at",
                )
            },
        }

    def distribution_binding(self, distribution_id):
        """Private attribution lookup; no host paths or workbench IDs returned."""
        with self.store.connect() as c:
            row = c.execute(
                "SELECT data FROM community_distributions WHERE id=%s",
                (identifier(distribution_id),),
            ).fetchone()
        require(row is not None, "community_distribution_not_found", 404)
        return {
            "receipt": row["data"]["receipt"],
            "download_admissions": row["data"]["download_admissions"],
        }


def install_community_routes(app, settings, store, auth):
    service = Community(settings, store)

    def call(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except CommunityError as error:
            raise HTTPException(error.status, {"code": error.code}) from None
        except DiscoveryError as error:
            raise HTTPException(error.status, {"code": error.code}) from None
        except (ValueError, OSError, KeyError, TypeError):
            raise HTTPException(
                400, {"code": "community_input_or_integrity_invalid"}
            ) from None

    @app.get("/api/community/admin/settings", dependencies=auth)
    def settings_get():
        return call(service.settings_get)

    @app.put("/api/community/admin/settings", dependencies=auth)
    def configure(body: SettingsInput):
        return call(service.configure, body)

    @app.get("/api/community/admin/tokens", dependencies=auth)
    def tokens():
        return {"credentials": call(service.tokens)}

    @app.post("/api/community/admin/tokens", dependencies=auth)
    def issue(body: TokenInput):
        return call(service.issue_token, body)

    @app.post("/api/community/admin/tokens/{token_id}/revoke", dependencies=auth)
    def revoke(token_id: str):
        return call(service.revoke, token_id)

    @app.get("/api/community/admin/submissions", dependencies=auth)
    def submissions():
        return {"submissions": call(service.submissions)}

    @app.get("/api/community/admin/submissions/{submission_id}", dependencies=auth)
    def submission(submission_id: str):
        return call(service.get_submission, submission_id)

    @app.post(
        "/api/community/admin/submissions/{submission_id}/read", dependencies=auth
    )
    def read(submission_id: str, body: ReadInput):
        return call(service.read, submission_id, body)

    @app.post(
        "/api/community/admin/submissions/{submission_id}/preview", dependencies=auth
    )
    def preview(submission_id: str, body: PreviewInput):
        return call(service.preview, submission_id, body)

    @app.post(
        "/api/community/admin/submissions/{submission_id}/review", dependencies=auth
    )
    def review(submission_id: str, body: ReviewInput):
        return call(service.review, submission_id, body)

    @app.post(
        "/api/community/admin/submissions/{submission_id}/publish", dependencies=auth
    )
    def publish(submission_id: str, body: PublishInput):
        return call(service.publish, submission_id, body)

    @app.get("/api/community/admin/releases", dependencies=auth)
    def admin_releases():
        return {"releases": call(service.releases, admin=True)}

    @app.post("/api/community/admin/releases/{release_id}/withdraw", dependencies=auth)
    def withdraw(release_id: str, body: WithdrawInput):
        return call(service.withdraw, release_id, body)

    @app.post("/api/community/submissions/usage")
    def usage(body: UsageInput, authorization: str | None = Header(default=None)):
        return call(service.self_report, authorization, body)

    @app.post("/api/community/submissions")
    def submit(body: SubmissionInput, authorization: str | None = Header(default=None)):
        return call(service.submit, authorization, body)

    @app.get("/api/community/submissions")
    def own_submissions(authorization: str | None = Header(default=None)):
        return {"submissions": call(service.submissions, authorization or "")}

    @app.get("/api/community/submissions/{submission_id}")
    def own_submission(
        submission_id: str, authorization: str | None = Header(default=None)
    ):
        return call(service.get_submission, submission_id, authorization or "")

    @app.get("/api/community/public/site")
    def site():
        return call(service.public_site)

    @app.get("/api/community/public/releases")
    def releases():
        return {"releases": call(service.releases)}

    @app.get("/api/community/public/releases/{release_id}")
    def release(release_id: str):
        return call(service.release, release_id)

    @app.post("/api/community/public/releases/{release_id}/distributions")
    def distribute(release_id: str, body: DistributionInput):
        return call(service.distribute, release_id, body)

    @app.get("/api/community/public/distributions/{distribution_id}/download")
    def download(distribution_id: str):
        return Response(
            call(service.download, distribution_id),
            media_type="application/gzip",
            headers={
                "Content-Disposition": 'attachment; filename="doonex-community-skill.tar.gz"',
                "Cache-Control": "no-store",
            },
        )

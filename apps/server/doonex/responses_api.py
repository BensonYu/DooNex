"""Authenticated, explicit recovery of durable local helper fences."""

from typing import Literal
from uuid import UUID

from pydantic import Field

from doonex.models import StrictModel
from doonex.responses_registry import inspect_helper, list_unresolved, resolve_helper


class LeaseInput(StrictModel):
    device: int = Field(strict=True, ge=0, le=2**63 - 1)
    inode: int = Field(strict=True, ge=1, le=2**63 - 1)


class ResolveInput(StrictModel):
    expected_record_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    attempt_id: UUID
    nonce: str = Field(pattern=r"^[a-f0-9]{64}$")
    lease_identity: LeaseInput
    reason: Literal["operator_confirmed_local_lease"]


def install_responses_routes(app, settings, store, auth):
    @app.get("/api/transport-helpers", dependencies=auth)
    def helpers(after_id: UUID | None = None):
        with store.connect() as connection:
            return list_unresolved(
                connection, after_id=str(after_id) if after_id else None
            )

    @app.get("/api/transport-helpers/{action_id}", dependencies=auth)
    def inspect(action_id: UUID):
        return inspect_helper(settings, store, str(action_id))

    @app.post("/api/transport-helpers/{action_id}/resolve", dependencies=auth)
    def resolve(action_id: UUID, body: ResolveInput):
        return resolve_helper(
            settings,
            store,
            str(action_id),
            expected_record_sha256=body.expected_record_sha256,
            attempt_id=str(body.attempt_id),
            nonce=body.nonce,
            lease_identity=body.lease_identity.model_dump(),
            reason=body.reason,
        )

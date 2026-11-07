"""Worker-owned connection between a reserved model action and one helper."""

import secrets
import time
from uuid import uuid4

from doonex.budget import elapsed
from doonex.responses_adapter import HelperReportedError
from doonex.responses_process import HelperHooks, HelperRequest, run_helper
from doonex.responses_protocol import assert_direct_environment, validate_call
from doonex.responses_registry import ResponsesRegistry, lease_spec
from doonex.responses_transport import rebuild_response

_PARENT_BOOT_ID = str(uuid4())
DEFAULT_API_BASE = "https://api.openai.com/v1"


class ResponsesBinding:
    def __init__(
        self,
        settings,
        store,
        *,
        run_id,
        owner,
        check_owner,
        check_dispatch,
        model,
        api_key,
    ):
        self.settings, self.store = settings, store
        self.run_id, self.owner, self.check_owner = run_id, owner, check_owner
        self.check_dispatch = check_dispatch
        self.model, self.api_key = dict(model), api_key
        self._entered: set[str] = set()

    def _registry(self, reservation):
        return ResponsesRegistry(
            self.settings,
            self.store,
            run_id=self.run_id,
            owner=self.owner,
            action_id=reservation["id"],
            check_owner=self.check_owner,
        )

    def _not_spawned(self, reservation, client_request_id):
        # Only the original live parent uses this path, before entering
        # run_helper. A restarted worker cannot infer this from an empty lock.
        return self._registry(reservation).block(
            {
                "schema": 1,
                "transport": "isolated_direct_v1",
                "attempt_id": str(uuid4()),
                "client_request_id": client_request_id,
                "nonce": secrets.token_hex(32),
                "previous_record_sha256": None,
                "spawn_attempted": False,
                "child_pid": None,
                "reaped": False,
                "errors": [],
                "reason": "preflight",
            }
        )

    def abandon_uninvoked(self, reservation):
        """SDK preparation failed before this binding was ever called."""
        if reservation["id"] not in self._entered:
            self._not_spawned(reservation, str(uuid4()))
            self._entered.add(reservation["id"])

    def __call__(self, reservation, sdk_call, client_request_id):
        action_id = reservation["id"]
        if action_id in self._entered:
            raise RuntimeError("A model reservation cannot dispatch twice")
        self._entered.add(action_id)
        invoked = False
        try:
            self.check_dispatch()
            assert_direct_environment()
            call = dict(sdk_call)
            api_key = call.pop("api_key", None)
            validate_call(call, client_request_id=client_request_id)
            expected_base = self.model.get("base_url") or DEFAULT_API_BASE
            if (
                self.model.get("responses_transport") != "isolated_direct_v1"
                or self.model.get("provider") != "openai"
                or self.model.get("api_mode") != "responses"
                or api_key != self.api_key
                or call["model"] != self.model["model_id"].removeprefix("openai/")
                or call["api_base"] != expected_base
                or (
                    expected_base.startswith("http:")
                    and not self.model.get("allow_private_http")
                )
                or call["timeout"] > self.model["timeout_seconds"]
                or call["max_output_tokens"] != self.model["max_output_tokens"]
                or reservation.get("transport_mode") != "isolated_direct_v1"
            ):
                raise ValueError(
                    "Responses request differs from its frozen configuration"
                )
            run = self.store.get(self.run_id)
            remaining = min(
                call["timeout"], run["request"]["budget"]["max_seconds"] - elapsed(run)
            )
            if remaining <= 0:
                raise ValueError("Responses request has no remaining time")
            deadline = time.monotonic_ns() + int(remaining * 1_000_000_000)
            lease = lease_spec(
                self.settings,
                self.store,
                check_owner=self.check_owner,
                exclude_action_id=action_id,
            )
            registry = self._registry(reservation)
            request = HelperRequest(
                call=call,
                api_key=self.api_key,
                deadline_monotonic_ns=deadline,
                lease=lease,
                parent_boot_id=_PARENT_BOOT_ID,
                client_request_id=client_request_id,
            )

            def register(payload):
                self.check_dispatch()
                return registry.register(payload)

            def arm(payload):
                self.check_dispatch()
                return registry.arm(payload)

            hooks = HelperHooks(register, arm, registry.complete, registry.block)
            # From here only run_helper can prove whether spawn was attempted.
            invoked = True
            terminal = run_helper(request, hooks)
        except BaseException:
            if not invoked:
                self._not_spawned(reservation, client_request_id)
            raise
        if terminal.envelope["kind"] == "error":
            raise HelperReportedError(terminal.envelope, secret=self.api_key)
        return rebuild_response(terminal.envelope)

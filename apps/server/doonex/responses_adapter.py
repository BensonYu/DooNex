"""One budget context per pinned synchronous SDK Responses invocation.

Install the alias once when the private SDK runtime loads. Never fall back to
direct transport when an isolated binding is absent or a context is lost.
The SDK still owns message conversion, response processing and conversation.
"""

from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import version
from threading import Lock


@dataclass
class _Dispatch:
    binding: Callable | None
    reservation: object
    client_request_id: str
    expected_tools: list | None
    used: bool = False
    completed: bool = False


_ACTIVE: ContextVar[_Dispatch | None] = ContextVar("doonex_responses", default=None)
_INSTALL_LOCK = Lock()
_ORIGINAL = None


def _dispatch(**kwargs):
    context = _ACTIVE.get()
    if context is None or context.used:
        raise RuntimeError("Responses requires one unused budget context")
    context.used = True
    if context.binding is None:
        return _ORIGINAL(**kwargs)
    if kwargs.get("tools") != context.expected_tools:
        raise ValueError("Responses tools differ from the authorized SDK tools")
    response = context.binding(context.reservation, kwargs, context.client_request_id)
    context.completed = True
    return response


def install() -> None:
    global _ORIGINAL
    with _INSTALL_LOCK:
        module = import_module("openhands.sdk.llm.llm")
        if _ORIGINAL is not None:
            if module.litellm_responses is not _dispatch:
                raise RuntimeError("Pinned SDK Responses alias has changed")
            return
        from litellm.responses.main import responses

        if (
            version("openhands-sdk") != "1.60.1"
            or module.litellm_responses is not responses
        ):
            raise RuntimeError("Pinned SDK Responses alias is unavailable")
        _ORIGINAL = responses
        module.litellm_responses = _dispatch


def assert_installed() -> None:
    if import_module("openhands.sdk.llm.llm").litellm_responses is not _dispatch:
        raise RuntimeError("Isolated Responses requires the pinned controller alias")


@contextmanager
def request_context(binding, reservation, client_request_id, expected_tools):
    if _ACTIVE.get() is not None:
        raise RuntimeError("Nested Responses requests are forbidden")
    if binding is not None:
        assert_installed()
    dispatch = _Dispatch(binding, reservation, client_request_id, expected_tools)
    token = _ACTIVE.set(dispatch)
    try:
        yield
        if binding is not None and not dispatch.completed:
            raise RuntimeError(
                "Isolated Responses returned without its helper dispatch"
            )
    finally:
        _ACTIVE.reset(token)


class HelperReportedError(RuntimeError):
    """A validated finite error envelope, never an invented HTTP exception."""

    def __init__(self, terminal, *, secret):
        from doonex.responses_protocol import (
            ERROR_REASONS,
            ERROR_TYPES,
            validate_diagnostics,
        )

        if (
            terminal.get("kind") != "error"
            or terminal.get("reason") not in ERROR_REASONS
            or terminal.get("error_type") not in ERROR_TYPES
        ):
            raise ValueError("Invalid helper error envelope")
        super().__init__("Isolated Responses transport failed")
        self.error_type = terminal["error_type"]
        self.diagnostics = {
            **validate_diagnostics(terminal["diagnostics"], secret=secret),
            "helper_reason": terminal["reason"],
        }

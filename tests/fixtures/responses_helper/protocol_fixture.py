"""Offline wire fixture.  It is never loaded by the product runtime."""

import json
import struct

FRAME_LIMITS = {"ready": 4096, "arm": 1048576, "terminal": 2097152}


class ProtocolError(ValueError):
    pass


def encode_frame(envelope, kind):
    payload = json.dumps(envelope, allow_nan=False, separators=(",", ":")).encode()
    if len(payload) > FRAME_LIMITS[kind]:
        raise ProtocolError("size")
    return struct.pack("!I", len(payload)) + payload


def decode_frame_payload(payload, kind, identity=None):
    value = json.loads(payload)
    if value.get("v") != 1 or value.get("transport") != "isolated_direct_v1":
        raise ProtocolError("version")
    if value.get("kind") not in ({"result", "error"} if kind == "terminal" else {kind}):
        raise ProtocolError("kind")
    if identity is not None and any(
        value.get(key) != val for key, val in identity.items()
    ):
        raise ProtocolError("identity")
    return value

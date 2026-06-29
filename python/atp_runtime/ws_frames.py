"""Minimal RFC 6455 WebSocket frame codec (text / close / ping / pong).

The operator-interface runtime needs a real WebSocket transport so the
dashboard (``SRS-UI-001``) can subscribe to the declared event channels over a
live socket — but pulling in a WebSocket dependency would violate the
"no new dependency without scope confirmation" rule, and the contract block
declares the runtime is framework-agnostic. So this module implements just the
slice of RFC 6455 the operator surface needs, in the standard library:

* :func:`compute_accept_key` — the ``Sec-WebSocket-Accept`` handshake value.
* :func:`encode_text_frame` / :func:`encode_close_frame` / :func:`encode_pong_frame`
  — unmasked server→client frames.
* :func:`decode_frame` — parse one (masked) client→server frame from a buffer,
  returning the frame and the unconsumed remainder.

Continuation frames, compression extensions, and fragmented messages are out
of scope: the operator control protocol exchanges small, single-frame JSON
messages. A fragmented or oversized client frame is rejected by the caller.

SRS trace
---------
``SRS-API-001`` (WebSocket operator surface, API-3), ``SRS-UI-001``.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from enum import IntEnum

#: RFC 6455 §1.3 handshake GUID.
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

#: Hard ceiling on a single client frame payload. The control protocol sends
#: small JSON; anything larger is treated as a protocol violation rather than
#: buffered unbounded.
MAX_FRAME_PAYLOAD = 1 << 20  # 1 MiB


class OpCode(IntEnum):
    """The RFC 6455 opcodes the runtime handles."""

    CONTINUATION = 0x0
    TEXT = 0x1
    BINARY = 0x2
    CLOSE = 0x8
    PING = 0x9
    PONG = 0xA


class FrameError(Exception):
    """Raised on a malformed or unsupported client frame."""


@dataclass(frozen=True, slots=True)
class Frame:
    """One decoded WebSocket frame.

    Attributes:
        fin: Whether the FIN bit is set (always required for our messages).
        opcode: The frame :class:`OpCode`.
        payload: Unmasked payload bytes.
    """

    fin: bool
    opcode: OpCode
    payload: bytes

    @property
    def text(self) -> str:
        """Decode the payload as UTF-8 text (for TEXT frames)."""

        return self.payload.decode("utf-8")


def compute_accept_key(sec_websocket_key: str) -> str:
    """Return the ``Sec-WebSocket-Accept`` value for a client key.

    Example:
        >>> compute_accept_key("dGhlIHNhbXBsZSBub25jZQ==")
        's3pPLMBiTxaQ9kYGzzhZRbK+xOo='
    """

    digest = hashlib.sha1((sec_websocket_key + _WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _encode(opcode: OpCode, payload: bytes) -> bytes:
    """Encode a single unmasked (server→client) FIN frame."""

    header = bytearray()
    header.append(0x80 | int(opcode))  # FIN + opcode
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < (1 << 16):
        header.append(126)
        header += length.to_bytes(2, "big")
    else:
        header.append(127)
        header += length.to_bytes(8, "big")
    return bytes(header) + payload


def encode_text_frame(text: str) -> bytes:
    """Encode a server→client TEXT frame carrying ``text``."""

    return _encode(OpCode.TEXT, text.encode("utf-8"))


def encode_pong_frame(payload: bytes = b"") -> bytes:
    """Encode a server→client PONG frame echoing ``payload``."""

    return _encode(OpCode.PONG, payload)


def encode_close_frame(code: int = 1000, reason: str = "") -> bytes:
    """Encode a server→client CLOSE frame with a status ``code``."""

    body = code.to_bytes(2, "big") + reason.encode("utf-8")
    return _encode(OpCode.CLOSE, body)


def decode_frame(buffer: bytes, *, require_mask: bool = True) -> tuple[Frame | None, bytes]:
    """Parse one WebSocket frame from ``buffer``.

    Returns ``(frame, remaining)`` when a whole frame is available, or
    ``(None, buffer)`` when more bytes are needed. The server reads
    *client→server* frames, which MUST be masked (RFC 6455 §5.1): with the
    default ``require_mask=True`` an unmasked frame raises :class:`FrameError`.
    A client (e.g. the dashboard, or a test) reads *server→client* frames,
    which are unmasked: pass ``require_mask=False``.

    Example:
        >>> framed = _mask_for_test("hi")
        >>> frame, rest = decode_frame(framed)
        >>> frame.text, rest
        ('hi', b'')
        >>> server = encode_text_frame("yo")
        >>> decode_frame(server, require_mask=False)[0].text
        'yo'
    """

    if len(buffer) < 2:
        return None, buffer

    first, second = buffer[0], buffer[1]
    fin = bool(first & 0x80)
    opcode_bits = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    cursor = 2

    if length == 126:
        if len(buffer) < cursor + 2:
            return None, buffer
        length = int.from_bytes(buffer[cursor : cursor + 2], "big")
        cursor += 2
    elif length == 127:
        if len(buffer) < cursor + 8:
            return None, buffer
        length = int.from_bytes(buffer[cursor : cursor + 8], "big")
        cursor += 8

    if length > MAX_FRAME_PAYLOAD:
        raise FrameError(f"frame payload {length} exceeds {MAX_FRAME_PAYLOAD} byte ceiling")

    if require_mask and not masked:
        raise FrameError("client frame is not masked (RFC 6455 §5.1)")

    mask = b""
    if masked:
        if len(buffer) < cursor + 4:
            return None, buffer
        mask = buffer[cursor : cursor + 4]
        cursor += 4

    if len(buffer) < cursor + length:
        return None, buffer
    raw_payload = buffer[cursor : cursor + length]
    cursor += length
    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(raw_payload)) if masked else raw_payload

    try:
        opcode = OpCode(opcode_bits)
    except ValueError as exc:
        raise FrameError(f"unsupported opcode 0x{opcode_bits:x}") from exc

    return Frame(fin=fin, opcode=opcode, payload=payload), buffer[cursor:]


def _mask_for_test(text: str, mask: bytes = b"\x01\x02\x03\x04") -> bytes:
    """Build a masked client TEXT frame (test/doctest helper)."""

    payload = text.encode("utf-8")
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    header = bytes([0x80 | int(OpCode.TEXT), 0x80 | len(payload)])
    return header + mask + masked

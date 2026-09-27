"""Bounded D-Bus framing. Descriptors are counted and closed, never transported."""

import array
from dataclasses import dataclass
import os
import socket
import struct

MAX_MESSAGE_BYTES = 32 * 1024 * 1024
MAX_HEADER_BYTES = 1024 * 1024
MAX_FDS = 256


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class Frame:
    data: bytes
    endian: str
    message_type: int
    flags: int
    serial: int
    fields: dict
    fd_count: int = 0


def receive(sock, size):
    """Receive no more than size bytes; close every installed SCM_RIGHTS fd."""
    data, ancillary, flags, _ = sock.recvmsg(
        size, socket.CMSG_SPACE(MAX_FDS * array.array("i").itemsize),
        getattr(socket, "MSG_CMSG_CLOEXEC", 0),
    )
    count = 0
    invalid = False
    for level, kind, payload in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            fds = array.array("i")
            whole = len(payload) - len(payload) % fds.itemsize
            fds.frombytes(payload[:whole])
            for fd in fds:
                os.close(fd)
                count += 1
            invalid |= whole != len(payload)
        else:
            invalid = True
    if invalid or flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC):
        raise ProtocolError("invalid or truncated ancillary data")
    return data, count


def exact(sock, size, allow_eof=False):
    result = bytearray()
    fds = 0
    while len(result) < size:
        data, count = receive(sock, min(size - len(result), 65536))
        fds += count
        if fds > MAX_FDS:
            raise ProtocolError("too many descriptors in message")
        if not data:
            if not result and allow_eof and not fds:
                return None, 0
            raise ProtocolError("truncated D-Bus message")
        result.extend(data)
    return bytes(result), fds


def signature_types(signature):
    """Parse a D-Bus signature, including unknown header field value types."""
    def one(pos, depth, dictionary=False):
        if depth > 32 or pos >= len(signature):
            raise ProtocolError("invalid type signature")
        char = signature[pos]
        if char in "ybnqiuxtdhsogv":
            return char, pos + 1
        if char == "a":
            item, end = one(pos + 1, depth + 1, True)
            return ("a", item), end
        if char in "({":
            if char == "{" and not dictionary:
                raise ProtocolError("dictionary entry outside array")
            end_char = ")" if char == "(" else "}"
            parts = []
            pos += 1
            while pos < len(signature) and signature[pos] != end_char:
                item, pos = one(pos, depth + 1)
                parts.append(item)
            if not parts or pos >= len(signature):
                raise ProtocolError("unterminated or empty structure")
            if char == "{" and (len(parts) != 2 or not isinstance(parts[0], str)
                                or parts[0] not in "ybnqiuxtdhsog"):
                raise ProtocolError("invalid dictionary entry")
            return (char, tuple(parts)), pos + 1
        raise ProtocolError("unknown signature type")

    if len(signature) > 255:
        raise ProtocolError("signature too long")
    out = []
    pos = 0
    while pos < len(signature):
        item, pos = one(pos, 0)
        out.append(item)
    return out


class HeaderReader:
    def __init__(self, data, endian):
        self.data = data
        self.endian = endian
        self.pos = 16

    def take(self, length):
        end = self.pos + length
        if end > len(self.data):
            raise ProtocolError("header field exceeds header")
        out = self.data[self.pos:end]
        self.pos = end
        return out

    def align(self, alignment):
        if any(self.take((-self.pos) % alignment)):
            raise ProtocolError("nonzero header padding")

    def number(self, fmt):
        return struct.unpack(self.endian + fmt, self.take(struct.calcsize(fmt)))[0]

    def value(self, kind, depth=0):
        if depth > 32:
            raise ProtocolError("header nesting too deep")
        tag = kind if isinstance(kind, str) else kind[0]
        self.align({"y": 1, "g": 1, "v": 1, "n": 2, "q": 2,
                    "b": 4, "i": 4, "u": 4, "h": 4, "s": 4,
                    "o": 4, "a": 4, "x": 8, "t": 8, "d": 8,
                    "(": 8, "{": 8}[tag])
        if tag in "ybnqiuxtdh":
            value = self.number({"y": "B", "b": "I", "n": "h", "q": "H",
                                 "i": "i", "u": "I", "x": "q", "t": "Q",
                                 "d": "d", "h": "I"}[tag])
            if tag == "b" and value not in (0, 1):
                raise ProtocolError("invalid boolean")
            return value
        if tag in "sog":
            size = self.number("B" if tag == "g" else "I")
            raw = self.take(size)
            if self.take(1) != b"\0" or b"\0" in raw:
                raise ProtocolError("invalid string terminator")
            try:
                value = raw.decode("ascii" if tag == "g" else "utf-8")
            except UnicodeError as exc:
                raise ProtocolError("invalid header string") from exc
            if tag == "g":
                signature_types(value)
            return value
        if tag == "v":
            types = signature_types(self.value("g", depth + 1))
            if len(types) != 1:
                raise ProtocolError("variant must have exactly one type")
            return self.value(types[0], depth + 1)
        if tag in "({":
            return tuple(self.value(item, depth + 1) for item in kind[1])
        size = self.number("I")
        item = kind[1]
        # Array byte count excludes initial element alignment padding.
        align_tag = item if isinstance(item, str) else item[0]
        alignment = {"y": 1, "g": 1, "v": 1, "n": 2, "q": 2,
                     "b": 4, "i": 4, "u": 4, "h": 4, "s": 4,
                     "o": 4, "a": 4, "x": 8, "t": 8, "d": 8,
                     "(": 8, "{": 8}[align_tag]
        self.align(alignment)
        end = self.pos + size
        if end > len(self.data):
            raise ProtocolError("array exceeds header")
        while self.pos < end:
            self.value(item, depth + 1)
        if self.pos != end:
            raise ProtocolError("array length mismatch")
        return None


def read_frame(sock, max_message_bytes=MAX_MESSAGE_BYTES):
    fixed, fds = exact(sock, 16, allow_eof=True)
    if fixed is None:
        return None
    if fixed[0:1] not in (b"l", b"B") or fixed[3] != 1:
        raise ProtocolError("unsupported byte order or protocol version")
    endian = "<" if fixed[0:1] == b"l" else ">"
    message_type, flags = fixed[1], fixed[2]
    body_size, serial, header_size = struct.unpack(endian + "III", fixed[4:])
    if message_type not in (1, 2, 3, 4) or serial == 0:
        raise ProtocolError("invalid message type or serial")
    padded_header = (header_size + 7) & ~7
    total = 16 + padded_header + body_size
    if header_size > MAX_HEADER_BYTES or total > max_message_bytes:
        raise ProtocolError("D-Bus message exceeds configured limit")
    rest, more_fds = exact(sock, padded_header + body_size)
    fds += more_fds
    if fds > MAX_FDS:
        raise ProtocolError("too many descriptors in message")
    data = fixed + rest
    reader = HeaderReader(data[:16 + header_size], endian)
    fields = {}
    expected = {1: "o", 2: "s", 3: "s", 4: "s", 5: "u", 6: "s", 7: "s", 8: "g", 9: "u"}
    while reader.pos < len(reader.data):
        reader.align(8)
        field = reader.number("B")
        if field == 0 or field in fields:
            raise ProtocolError("zero or duplicate header field")
        signature = reader.value("g")
        types = signature_types(signature)
        if len(types) != 1 or (field in expected and signature != expected[field]):
            raise ProtocolError("invalid header field type")
        fields[field] = reader.value(types[0])
    required = {1: (1, 3), 2: (5,), 3: (4, 5), 4: (1, 2, 3)}[message_type]
    if any(field not in fields for field in required) or fields.get(5) == 0:
        raise ProtocolError("missing required header or invalid reply serial")
    if any(data[16 + header_size:16 + padded_header]):
        raise ProtocolError("nonzero body alignment padding")
    if fields.get(9, 0) != fds:
        raise ProtocolError("descriptor count does not match UNIX_FDS header")
    return Frame(data, endian, message_type, flags, serial, fields, fds)


def make_error(frame, serial=1):
    """Replace an unsupported call or reply with a normal D-Bus error."""
    reply_serial = frame.serial if frame.message_type == 1 else frame.fields[5]
    fields = bytearray()
    entries = [
        (4, "s", "org.freedesktop.DBus.Error.NotSupported"),
        (5, "u", reply_serial),
        (8, "g", "s"),
    ]
    destination = frame.fields.get(7 if frame.message_type == 1 else 6)
    if destination:
        entries.append((6, "s", destination))
    for code, signature, value in entries:
        fields.extend(b"\0" * ((-len(fields)) % 8))
        fields.extend(bytes((code, 1, ord(signature), 0)))
        if signature == "u":
            fields.extend(struct.pack("<I", value))
        elif signature == "s":
            raw = value.encode()
            fields.extend(struct.pack("<I", len(raw)) + raw + b"\0")
        else:
            fields.extend(bytes((len(value),)) + value.encode() + b"\0")
    message = b"Unix file descriptor transfer is not supported across this VM boundary"
    body = struct.pack("<I", len(message)) + message + b"\0"
    fixed = b"l\x03\x00\x01" + struct.pack("<III", len(body), serial, len(fields))
    return fixed + fields + b"\0" * ((-len(fields)) % 8) + body

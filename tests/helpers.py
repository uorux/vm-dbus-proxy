"""Independent D-Bus fixtures and duplex streams; no socket permissions needed."""
import array
import queue
import socket
import struct


def message(*, endian='<', kind=1, flags=0, serial=42, fields=None, body=b''):
    if fields is None:
        fields = {1: ('o', '/test'), 2: ('s', 'org.example.Test'),
                  3: ('s', 'Ping'), 6: ('s', 'org.example.Test')}
    header = bytearray()
    for code, (signature, value) in fields.items():
        header.extend(b'\0' * (-len(header) % 8))
        header.extend(bytes([code, 1, ord(signature), 0]))
        if signature in ('s', 'o'):
            encoded = value.encode()
            header.extend(struct.pack(endian + 'I', len(encoded)))
            header.extend(encoded + b'\0')
        elif signature == 'u':
            header.extend(struct.pack(endian + 'I', value))
        elif signature == 'g':
            encoded = value.encode()
            header.extend(bytes([len(encoded)]) + encoded + b'\0')
        else:
            raise ValueError(signature)
    fixed = struct.pack(endian + 'BBBBIII', ord('l' if endian == '<' else 'B'),
                        kind, flags, 1, len(body), serial, len(header))
    return fixed + header + b'\0' * (-len(header) % 8) + body


class Stream:
    def __init__(self):
        self.incoming = queue.Queue()
        self.pending = b''
        self.closed = False
        self.timeout = 3

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendall(self, data):
        self.peer.incoming.put((bytes(data), [], 0))

    def recvmsg(self, size, ancbufsize=0, flags=0):
        ancillary, msgflags = [], 0
        if not self.pending:
            self.pending, ancillary, msgflags = self.incoming.get(timeout=3)
        result, self.pending = self.pending[:size], self.pending[size:]
        return result, ancillary, msgflags, None

    def recv(self, size):
        return self.recvmsg(size)[0]

    def shutdown(self, how):
        # Like a real socket: SHUT_WR ends the peer's reads and SHUT_RD wakes
        # our own blocked reader with EOF.
        if how in (socket.SHUT_WR, socket.SHUT_RDWR):
            self.peer.incoming.put((b'', [], 0))
        if how in (socket.SHUT_RD, socket.SHUT_RDWR):
            self.incoming.put((b'', [], 0))

    def close(self):
        if not self.closed:
            self.closed = True
            self.shutdown(socket.SHUT_RDWR)


def stream_pair():
    first, second = Stream(), Stream()
    first.peer, second.peer = second, first
    return first, second


def feed(stream, data, descriptors=(), flags=0):
    ancillary = []
    if descriptors:
        ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                      array.array('i', descriptors).tobytes())]
    stream.incoming.put((data, ancillary, flags))


def line(stream):
    result = b''
    while not result.endswith(b'\r\n'):
        next_byte = stream.recv(1)
        if not next_byte:
            raise EOFError(result)
        result += next_byte
    return result

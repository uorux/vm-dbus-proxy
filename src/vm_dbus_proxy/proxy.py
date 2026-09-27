"""Transparent per-connection proxy; the host remains the bus and policy owner."""

import argparse
import logging
import os
import socket
import threading
import time

from .protocol import MAX_MESSAGE_BYTES, ProtocolError, make_error, read_frame, receive

LOG = logging.getLogger("vm-dbus-proxy")
MAX_AUTH_LINE = 16384


def auth_timeout(sock, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("D-Bus authentication deadline expired")
    sock.settimeout(remaining)


def auth_send(sock, data, deadline):
    auth_timeout(sock, deadline)
    sock.sendall(data)


def auth_line(sock, deadline):
    line = bytearray()
    while len(line) < MAX_AUTH_LINE:
        auth_timeout(sock, deadline)
        data, fds = receive(sock, 1)
        if fds:
            raise ProtocolError("descriptor received during authentication")
        if not data:
            raise ProtocolError("authentication disconnected")
        line.extend(data)
        if line.endswith(b"\r\n"):
            return bytes(line)
    raise ProtocolError("authentication line too long")


def authenticate(client, upstream, timeout=10):
    """Pass authentication to the host; negotiate descriptor receipt only here."""
    deadline = time.monotonic() + timeout
    auth_timeout(client, deadline)
    initial, fds = receive(client, 1)
    if initial != b"\0" or fds:
        raise ProtocolError("authentication must start with NUL without descriptors")
    auth_send(upstream, initial, deadline)
    authenticated = False
    fd_negotiated = False
    while True:
        line = auth_line(client, deadline)
        command = line[:-2].split(b" ", 1)[0]
        if command == b"NEGOTIATE_UNIX_FD":
            if authenticated and line == b"NEGOTIATE_UNIX_FD\r\n":
                fd_negotiated = True
                auth_send(client, b"AGREE_UNIX_FD\r\n", deadline)
            else:
                auth_send(client, b"ERROR Authenticate before negotiating descriptors\r\n", deadline)
            continue
        if command == b"BEGIN":
            if not authenticated or line != b"BEGIN\r\n":
                raise ProtocolError("invalid BEGIN")
            auth_send(upstream, line, deadline)
            return fd_negotiated
        if command in (b"CANCEL", b"AUTH"):
            # Authentication restarts invalidate any earlier local agreement.
            authenticated = False
            fd_negotiated = False
        auth_send(upstream, line, deadline)
        response = auth_line(upstream, deadline)
        status = response[:-2].split(b" ", 1)[0]
        if status == b"AGREE_UNIX_FD":
            raise ProtocolError("upstream unexpectedly negotiated descriptors")
        if status == b"OK":
            authenticated = True
        elif status == b"REJECTED":
            authenticated = False
            fd_negotiated = False
        auth_send(client, response, deadline)


def bridge(client, upstream, max_message_bytes=MAX_MESSAGE_BYTES):
    """Run one app connection; names, serials and match rules stay host-owned."""
    client_write = threading.Lock()
    upstream_write = threading.Lock()
    stopped = threading.Event()
    # Synthetic errors never request replies. Their serials are not used to
    # match pending calls (REPLY_SERIAL is); host frames remain byte-for-byte.
    next_error_serial = 0x80000000

    def shutdown():
        stopped.set()
        for connection in (client, upstream):
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def forward_host():
        try:
            while not stopped.is_set():
                frame = read_frame(upstream, max_message_bytes)
                if frame is None:
                    break
                if frame.fd_count:
                    raise ProtocolError("upstream sent descriptors without negotiation")
                with client_write:
                    client.sendall(frame.data)
        except (OSError, ProtocolError) as exc:
            if not stopped.is_set():
                LOG.warning("host connection: %s", exc)
        finally:
            shutdown()

    host_thread = None
    try:
        client.settimeout(10)
        upstream.settimeout(10)
        negotiated = authenticate(client, upstream)
        client.settimeout(None)
        upstream.settimeout(None)
        host_thread = threading.Thread(target=forward_host, daemon=True)
        host_thread.start()
        while not stopped.is_set():
            frame = read_frame(client, max_message_bytes)
            if frame is None:
                break
            if frame.fd_count:
                if not negotiated:
                    raise ProtocolError("client sent descriptors without negotiation")
                LOG.info("unsupported descriptor message: type=%s member=%s count=%s",
                         frame.message_type, frame.fields.get(3, ""), frame.fd_count)
                if frame.message_type == 1 and not frame.flags & 1:
                    with client_write:
                        client.sendall(make_error(frame, next_error_serial))
                    next_error_serial = (next_error_serial + 1) & 0xffffffff or 1
                elif frame.message_type in (2, 3):
                    # A reply's original serial remains valid on this app's
                    # upstream connection, and its destination routes the error.
                    with upstream_write:
                        upstream.sendall(make_error(frame, frame.serial))
                continue
            with upstream_write:
                upstream.sendall(frame.data)
    except (OSError, ProtocolError) as exc:
        if not stopped.is_set():
            LOG.warning("client connection: %s", exc)
    finally:
        shutdown()
        if host_thread is not None:
            host_thread.join()
        client.close()
        upstream.close()


def serve(listen, upstream, max_message_bytes=MAX_MESSAGE_BYTES):
    """Listen only on a guest Unix socket, forwarding to its existing relay."""
    if os.path.lexists(listen):
        # Never silently steal another service's socket or remove other files.
        raise RuntimeError(f"listener path already exists: {listen}")
    os.makedirs(os.path.dirname(os.path.abspath(listen)), exist_ok=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(listen)
    os.chmod(listen, 0o600)
    listener.listen(64)
    slots = threading.BoundedSemaphore(128)

    def handle(client):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            deadline = time.monotonic() + 5
            while True:
                try:
                    connection.connect(upstream)
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
            bridge(client, connection, max_message_bytes)
        except OSError as exc:
            LOG.warning("cannot connect to upstream: %s", exc)
        finally:
            client.close()
            connection.close()
            slots.release()

    LOG.info("listening on %s", listen)
    try:
        while True:
            client, _ = listener.accept()
            if not slots.acquire(blocking=False):
                client.close()
                continue
            threading.Thread(target=handle, args=(client,), daemon=True).start()
    finally:
        listener.close()
        os.unlink(listen)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", required=True)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--max-message-bytes", type=int, default=MAX_MESSAGE_BYTES)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if not 16 <= args.max_message_bytes <= 128 * 1024 * 1024:
        parser.error("message limit must be between 16 bytes and 128 MiB")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    serve(args.listen, args.upstream, args.max_message_bytes)

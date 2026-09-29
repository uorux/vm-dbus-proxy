import errno
import os
import queue
import socket
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

from vm_dbus_proxy import protocol, proxy
from helpers import feed, line, message, stream_pair


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.client, self.front = stream_pair()
        self.back, self.server = stream_pair()
        self.errors = queue.Queue()

        def run():
            try:
                proxy.bridge(self.front, self.back)
            except Exception as exc:
                self.errors.put(exc)

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.client.close()
        self.server.close()
        self.thread.join(4)
        self.assertFalse(self.thread.is_alive(), 'bridge leaked connection thread')

    def authenticate(self, negotiate=True):
        self.client.sendall(b'\0AUTH EXTERNAL 31303031\r\n')
        self.assertEqual(self.server.recv(1), b'\0')
        self.assertEqual(line(self.server), b'AUTH EXTERNAL 31303031\r\n')
        self.server.sendall(b'OK 0123456789abcdef0123456789abcdef\r\n')
        self.assertTrue(line(self.client).startswith(b'OK '))
        if negotiate:
            self.client.sendall(b'NEGOTIATE_UNIX_FD\r\n')
            self.assertEqual(line(self.client), b'AGREE_UNIX_FD\r\n')
        # BEGIN must be the very next upstream command: FD agreement is local.
        self.client.sendall(b'BEGIN\r\n')
        self.assertEqual(line(self.server), b'BEGIN\r\n')

    def test_no_fd_call_reply_signal_are_byte_exact(self):
        self.authenticate()
        call = message(endian='>', serial=51)
        self.client.sendall(call)
        self.assertEqual(protocol.read_frame(self.server).data, call)
        reply = message(kind=2, serial=90, fields={5: ('u', 51)})
        signal = message(kind=4, serial=91)
        self.server.sendall(reply + signal)
        self.assertEqual(protocol.read_frame(self.client).data, reply)
        self.assertEqual(protocol.read_frame(self.client).data, signal)
        self.assertTrue(self.errors.empty())

    def send_fd(self, *, kind=1, flags=0, serial=55):
        descriptor, other = os.pipe()
        self.addCleanup(os.close, other)
        fields = {1: ('o', '/test'), 2: ('s', 'org.example.Test'), 3: ('s', 'Take'), 9: ('u', 1)}
        feed(self.front, message(kind=kind, flags=flags, serial=serial, fields=fields), [descriptor])
        return descriptor

    def test_fd_call_errors_locally_and_connection_survives(self):
        self.authenticate()
        descriptor = self.send_fd()
        error = protocol.read_frame(self.client)
        self.assertEqual(error.fields[4], 'org.freedesktop.DBus.Error.NotSupported')
        self.assertEqual(error.fields[5], 55)
        with self.assertRaises(OSError):
            os.fstat(descriptor)
        following = message(serial=56)
        self.client.sendall(following)
        self.assertEqual(protocol.read_frame(self.server).data, following)
        reply = message(kind=2, serial=88, fields={5: ('u', 56)})
        self.server.sendall(reply)
        self.assertEqual(protocol.read_frame(self.client).data, reply)

    def test_fd_signal_and_no_reply_call_dropped_without_desync(self):
        self.authenticate()
        signal_fd = self.send_fd(kind=4)
        call_fd = self.send_fd(flags=1)
        following = message(serial=57)
        self.client.sendall(following)
        self.assertEqual(protocol.read_frame(self.server).data, following)
        for descriptor in (signal_fd, call_fd):
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        reply = message(kind=2, serial=88, fields={5: ('u', 57)})
        self.server.sendall(reply)
        self.assertEqual(protocol.read_frame(self.client).data, reply)

    def test_fd_reply_returns_error_to_original_host_caller(self):
        self.authenticate()
        descriptor, other = os.pipe()
        self.addCleanup(os.close, other)
        data = message(kind=2, serial=66,
                       fields={5: ('u', 32), 6: ('s', ':1.17'), 9: ('u', 1)})
        feed(self.front, data, [descriptor])
        error = protocol.read_frame(self.server)
        self.assertEqual(error.message_type, 3)
        self.assertEqual(error.serial, 66)
        self.assertEqual(error.fields[5], 32)
        self.assertEqual(error.fields[6], ':1.17')
        self.assertEqual(error.fd_count, 0)
        with self.assertRaises(OSError):
            os.fstat(descriptor)

    def assert_disconnected(self):
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.client.recv(1), b'')

    def test_fd_calls_get_distinct_error_serials(self):
        self.authenticate()
        serials = []
        for serial in (70, 71):
            self.send_fd(serial=serial)
            error = protocol.read_frame(self.client)
            self.assertEqual(error.fields[5], serial)
            serials.append(error.serial)
        self.assertNotEqual(serials[0], serials[1])

    def test_descriptors_without_negotiation_disconnect(self):
        self.authenticate(negotiate=False)
        descriptor = self.send_fd()
        self.assert_disconnected()
        with self.assertRaises(OSError):
            os.fstat(descriptor)

    def test_upstream_unix_fds_header_disconnects(self):
        self.authenticate()
        # The host transport cannot carry descriptors, so a declared count
        # without descriptors is a broken or hostile peer.
        self.server.sendall(message(kind=4, serial=5, fields={
            1: ('o', '/test'), 2: ('s', 'org.example.Test'), 3: ('s', 'Signal'),
            9: ('u', 1)}))
        self.assert_disconnected()

    def test_oversized_client_frame_disconnects_before_body(self):
        self.authenticate()
        data = bytearray(message(serial=60))
        struct.pack_into('<I', data, 4, 64 * 1024 * 1024)
        self.client.sendall(bytes(data[:16]))
        self.assert_disconnected()

    def test_begin_before_authentication_fails(self):
        self.client.sendall(b'\0BEGIN\r\n')
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.client.recv(1), b'')


class ServeTests(unittest.TestCase):
    def test_accept_failure_does_not_stop_listener(self):
        class Stop(BaseException):
            pass

        directory = tempfile.mkdtemp(prefix='vdp', dir='/tmp')
        listen, upstream = directory + '/bus', directory + '/up'
        host = socket.socket(socket.AF_UNIX)
        host.bind(upstream)
        host.listen(1)
        host.settimeout(5)
        release = threading.Event()
        real_accept = socket.socket.accept
        calls = []

        def accept(sock):
            calls.append(None)
            if len(calls) == 1:
                raise OSError(errno.EMFILE, 'Too many open files')
            if len(calls) == 3:
                release.wait(5)
                raise Stop()
            return real_accept(sock)

        def run():
            try:
                proxy.serve(listen, upstream)
            except Stop:
                pass

        with patch.object(socket.socket, 'accept', accept):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            try:
                for _ in range(100):
                    if os.path.exists(listen):
                        break
                    threading.Event().wait(0.02)
                with socket.socket(socket.AF_UNIX) as app:
                    app.settimeout(5)
                    app.connect(listen)
                    app.sendall(b'\0AUTH EXTERNAL 31303031\r\n')
                    server, _ = real_accept(host)
                    with server:
                        server.settimeout(5)
                        self.assertEqual(server.recv(1), b'\0')
                        self.assertEqual(line(server), b'AUTH EXTERNAL 31303031\r\n')
            finally:
                release.set()
                thread.join(5)
                host.close()
        self.assertFalse(thread.is_alive())
        self.assertFalse(os.path.exists(listen))
        os.unlink(upstream)
        os.rmdir(directory)


if __name__ == '__main__':
    unittest.main()

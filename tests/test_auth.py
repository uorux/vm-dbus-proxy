import itertools
import unittest
from unittest.mock import patch

from vm_dbus_proxy import protocol, proxy
from helpers import feed, line, message, stream_pair


class AuthenticationTests(unittest.TestCase):
    def streams(self, commands, responses):
        client, app = stream_pair()
        upstream, host = stream_pair()
        feed(client, commands)
        feed(upstream, responses)
        return client, app, upstream, host

    def test_slow_auth_cannot_extend_absolute_deadline(self):
        client, app, upstream, host = self.streams(
            b'\0AUTH EXTERNAL 31303031\r\n', b'OK abc\r\n')
        # Every byte arrives, but the accumulated elapsed time exceeds the
        # deadline. A per-read timeout alone would permit this indefinitely.
        with patch.object(proxy.time, 'monotonic', side_effect=itertools.count(0, 1)):
            with self.assertRaisesRegex(TimeoutError, 'deadline expired'):
                proxy.authenticate(client, upstream, timeout=5)
        self.assertLess(client.timeout, 5)
        self.assertGreater(client.timeout, 0)

    def test_cancel_revokes_authentication_and_fd_agreement(self):
        client, app, upstream, host = self.streams(
            b'\0AUTH EXTERNAL 31303031\r\nNEGOTIATE_UNIX_FD\r\n'
            b'CANCEL\r\nNEGOTIATE_UNIX_FD\r\nBEGIN\r\n',
            b'OK abc\r\nERROR canceled\r\n')
        with self.assertRaisesRegex(protocol.ProtocolError, 'invalid BEGIN'):
            proxy.authenticate(client, upstream)
        self.assertEqual(line(app), b'OK abc\r\n')
        self.assertEqual(line(app), b'AGREE_UNIX_FD\r\n')
        self.assertEqual(line(app), b'ERROR canceled\r\n')
        self.assertTrue(line(app).startswith(b'ERROR Authenticate'))

    def test_reauthentication_requires_fresh_fd_negotiation(self):
        client, app, upstream, host = self.streams(
            b'\0AUTH EXTERNAL 31303031\r\nNEGOTIATE_UNIX_FD\r\n'
            b'AUTH EXTERNAL 31303031\r\nBEGIN\r\n',
            b'OK abc\r\nOK def\r\n')
        self.assertFalse(proxy.authenticate(client, upstream))
        self.assertEqual(host.recv(1), b'\0')
        self.assertEqual(line(host), b'AUTH EXTERNAL 31303031\r\n')
        self.assertEqual(line(host), b'AUTH EXTERNAL 31303031\r\n')
        self.assertEqual(line(host), b'BEGIN\r\n')

    def test_pipelined_sd_bus_auth_keeps_first_message(self):
        # sd-bus writes its whole auth exchange and Hello without waiting.
        hello = message(serial=1, fields={1: ('o', '/org/freedesktop/DBus'),
                                          2: ('s', 'org.freedesktop.DBus'),
                                          3: ('s', 'Hello'),
                                          6: ('s', 'org.freedesktop.DBus')})
        client, app, upstream, host = self.streams(
            b'\0AUTH EXTERNAL\r\nDATA\r\nNEGOTIATE_UNIX_FD\r\nBEGIN\r\n' + hello,
            b'DATA\r\nOK abc\r\n')
        self.assertTrue(proxy.authenticate(client, upstream))
        self.assertEqual(line(app), b'DATA\r\n')
        self.assertEqual(line(app), b'OK abc\r\n')
        self.assertEqual(line(app), b'AGREE_UNIX_FD\r\n')
        self.assertEqual(host.recv(1), b'\0')
        # FD negotiation never reaches the host transport.
        self.assertEqual(line(host), b'AUTH EXTERNAL\r\n')
        self.assertEqual(line(host), b'DATA\r\n')
        self.assertEqual(line(host), b'BEGIN\r\n')
        self.assertEqual(protocol.read_frame(client).data, hello)

    def test_negotiation_before_ok_is_refused_locally(self):
        client, app, upstream, host = self.streams(
            b'\0NEGOTIATE_UNIX_FD\r\nAUTH EXTERNAL 31303031\r\nBEGIN\r\n', b'OK abc\r\n')
        self.assertFalse(proxy.authenticate(client, upstream))
        self.assertTrue(line(app).startswith(b'ERROR '))
        self.assertEqual(host.recv(1), b'\0')
        self.assertEqual(line(host), b'AUTH EXTERNAL 31303031\r\n')

    def test_upstream_fd_agreement_is_fatal(self):
        client, app, upstream, host = self.streams(
            b'\0AUTH EXTERNAL 31303031\r\nNEGOTIATE_UNIX_FD\t\r\n',
            b'OK abc\r\nAGREE_UNIX_FD\r\n')
        with self.assertRaisesRegex(protocol.ProtocolError, 'negotiated descriptors'):
            proxy.authenticate(client, upstream)


if __name__ == '__main__':
    unittest.main()

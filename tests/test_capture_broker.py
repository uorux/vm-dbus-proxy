import array
import json
import os
import socket
import unittest
from vm_dbus_proxy.capture_broker import receive_request, read_status, PIPEWIRE_CONF


class BrokerTests(unittest.TestCase):
    def test_request_failure_closes_all_received_descriptors(self):
        for data, flags, extra in [(b'not json', 0, []), (b'{}', socket.MSG_CTRUNC, []),
                                   (b'{}', 0, [(999, 999, b'')])]:
            with self.subTest(data=data, flags=flags, extra=extra):
                readfd, writefd = os.pipe()
                class Connection:
                    def recvmsg(self, *args):
                        return data, extra + [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                               array.array('i', [readfd]).tobytes())], flags, None
                try:
                    with self.assertRaises(ValueError):
                        receive_request(Connection())
                    with self.assertRaises(OSError):
                        os.fstat(readfd)
                finally:
                    os.close(writefd)

    def test_status_validates_node_type_and_range(self):
        for node in [True, 0, -1, 0xffffffff, '12', 12]:
            with self.subTest(node=node):
                readfd, writefd = os.pipe()
                try:
                    os.write(writefd, json.dumps({'node': node}).encode() + b'\n')
                    if node == 12:
                        self.assertEqual(read_status(readfd, None), 12)
                    else:
                        with self.assertRaises(ValueError):
                            read_status(readfd, None)
                finally:
                    os.close(readfd)
                    os.close(writefd)

    def test_status_eof_does_not_hang(self):
        readfd, writefd = os.pipe()
        os.close(writefd)
        try:
            with self.assertRaises(RuntimeError):
                read_status(readfd, None)
        finally:
            os.close(readfd)

    def test_private_pipewire_loads_no_plugins_by_client_name(self):
        # spa-node-factory and adapter both honour a client's library.name.
        for module in ('spa-node-factory', 'adapter', 'spa-device-factory'):
            self.assertNotIn('libpipewire-module-' + module, PIPEWIRE_CONF)
        # Per-socket access; access.force is ignored (legacy flatpak detection).
        self.assertIn('access.socket', PIPEWIRE_CONF)
        self.assertNotIn('access.force', PIPEWIRE_CONF)

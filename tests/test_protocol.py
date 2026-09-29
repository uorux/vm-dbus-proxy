import os
import socket
import struct
import unittest

from vm_dbus_proxy import protocol
from helpers import feed, message, stream_pair


class FramingTests(unittest.TestCase):
    def read(self, data, **kwargs):
        stream, peer = stream_pair()
        feed(stream, data)
        feed(stream, b'')
        return protocol.read_frame(stream, **kwargs)

    def test_fragmented_big_endian_frame_and_next_frame(self):
        stream, peer = stream_pair()
        data = message(endian='>', serial=0x12345678, body=b'\0\xffpayload')
        for byte in data:
            feed(stream, bytes([byte]))
        second = message(serial=19)
        feed(stream, second)
        self.assertEqual(protocol.read_frame(stream).data, data)
        frame = protocol.read_frame(stream)
        self.assertEqual(frame.data, second)
        self.assertEqual(frame.serial, 19)
        self.assertEqual(frame.fields[3], 'Ping')
        self.assertEqual(frame.fd_count, 0)

    def test_coalesced_frames_keep_boundaries(self):
        stream, peer = stream_pair()
        first, second = message(serial=1), message(serial=2)
        feed(stream, first + second)
        self.assertEqual(protocol.read_frame(stream).data, first)
        self.assertEqual(protocol.read_frame(stream).data, second)

    def test_eof_only_valid_at_frame_boundary(self):
        self.assertIsNone(self.read(b''))
        for cut in (1, 15, 20, len(message()) - 1):
            with self.subTest(cut=cut), self.assertRaises((protocol.ProtocolError, EOFError)):
                self.read(message()[:cut])

    def test_frame_cap_checked_before_receiving_body(self):
        data = bytearray(message())
        struct.pack_into('<I', data, 4, 0xffffffff)
        with self.assertRaises(protocol.ProtocolError):
            self.read(bytes(data[:16]), max_message_bytes=1024)

    def test_invalid_fixed_headers(self):
        for offset, value in ((0, ord('x')), (1, 0), (1, 5), (3, 2)):
            data = bytearray(message())
            data[offset] = value
            with self.subTest(offset=offset, value=value), self.assertRaises(protocol.ProtocolError):
                self.read(bytes(data))

    def test_descriptor_is_closed_and_count_preserved(self):
        descriptor, other = os.pipe()
        self.addCleanup(os.close, other)
        stream, peer = stream_pair()
        data = message(fields={1: ('o', '/test'), 3: ('s', 'Take'), 9: ('u', 1)})
        feed(stream, data, [descriptor])
        frame = protocol.read_frame(stream)
        self.assertEqual(frame.fd_count, 1)
        with self.assertRaises(OSError):
            os.fstat(descriptor)

    def test_descriptor_count_mismatch_fails_closed(self):
        for declared, received in ((1, 0), (0, 1)):
            stream, peer = stream_pair()
            descriptors = []
            if received:
                descriptor, other = os.pipe()
                self.addCleanup(os.close, other)
                descriptors.append(descriptor)
            data = message(fields={1: ('o', '/test'), 3: ('s', 'Take'), 9: ('u', declared)})
            feed(stream, data, descriptors)
            with self.subTest(declared=declared), self.assertRaises(protocol.ProtocolError):
                protocol.read_frame(stream)
            for descriptor in descriptors:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)

    def test_truncated_ancillary_closes_received_descriptors(self):
        descriptor, other = os.pipe()
        self.addCleanup(os.close, other)
        stream, peer = stream_pair()
        feed(stream, message(), [descriptor], socket.MSG_CTRUNC)
        with self.assertRaises(protocol.ProtocolError):
            protocol.read_frame(stream)
        with self.assertRaises(OSError):
            os.fstat(descriptor)

    def test_malformed_header_type_and_terminator(self):
        wrong_type = message(fields={1: ('s', '/test'), 3: ('s', 'Ping')})
        wrong_terminator = bytearray(message())
        # First header field is the object path "/test".
        wrong_terminator[29] = 1
        for data in (wrong_type, bytes(wrong_terminator)):
            with self.assertRaises(protocol.ProtocolError):
                self.read(data)

    def test_unknown_header_field_is_preserved(self):
        data = message(fields={1: ('o', '/test'), 3: ('s', 'Ping'), 42: ('s', 'future')})
        self.assertEqual(self.read(data).data, data)

    def test_truncated_frame_still_closes_descriptors(self):
        descriptor, other = os.pipe()
        self.addCleanup(os.close, other)
        stream, peer = stream_pair()
        feed(stream, message()[:5], [descriptor])
        feed(stream, b'')
        with self.assertRaises(protocol.ProtocolError):
            protocol.read_frame(stream)
        with self.assertRaises(OSError):
            os.fstat(descriptor)

    def test_error_reply_has_matching_serial_and_no_fds(self):
        for endian in ('<', '>'):
            frame = self.read(message(endian=endian, serial=175))
            reply = self.read(protocol.make_error(frame, serial=29))
            self.assertEqual(reply.message_type, 3)
            self.assertEqual(reply.serial, 29)
            self.assertEqual(reply.fields[5], 175)
            self.assertEqual(reply.fields[4], 'org.freedesktop.DBus.Error.NotSupported')
            self.assertEqual(reply.fd_count, 0)

    def test_signature_nesting_limits_are_per_kind(self):
        # 32 arrays plus 32 structures is valid D-Bus; 33 of either is not.
        deep = 'a' * 32 + '(' * 32 + 'y' + ')' * 32
        self.assertEqual(len(protocol.signature_types(deep)), 1)
        for bad in ('a' * 33 + 'y', '(' * 33 + 'y' + ')' * 33):
            with self.subTest(bad=bad[:40]), self.assertRaises(protocol.ProtocolError):
                protocol.signature_types(bad)

    def test_deep_body_signature_header_is_accepted(self):
        signature = 'a' * 20 + '(' * 20 + 's' + ')' * 20
        data = message(fields={1: ('o', '/test'), 3: ('s', 'Ping'), 8: ('g', signature)})
        self.assertEqual(self.read(data).fields[8], signature)

    def test_error_to_host_caller_keeps_destination(self):
        reply = self.read(message(kind=2, serial=66, fields={5: ('u', 32), 6: ('s', ':1.17')}))
        error = self.read(protocol.make_error(reply, serial=66))
        self.assertEqual((error.serial, error.fields[5], error.fields[6]), (66, 32, ':1.17'))


if __name__ == '__main__':
    unittest.main()

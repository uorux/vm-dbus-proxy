"""Optional real daemon smoke test. No session bus or host services are touched."""
import array
import contextlib
import os
import pwd
import queue
import select
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from vm_dbus_proxy import protocol
from vm_dbus_proxy.proxy import bridge
from helpers import message

BUS = {1: ('o', '/org/freedesktop/DBus'), 2: ('s', 'org.freedesktop.DBus'),
       6: ('s', 'org.freedesktop.DBus')}


@unittest.skipUnless(shutil.which('dbus-daemon') and shutil.which('dbus-send'),
                     'requires dbus-daemon and dbus-send')
class PrivateBusTests(unittest.TestCase):
    @contextlib.contextmanager
    def private_bus(self):
        """Yield a proxy socket path bridged to a private dbus-daemon."""
        try:
            pwd.getpwuid(os.getuid())
        except KeyError:
            # dbus-daemon cannot register clients whose uid it cannot resolve.
            self.skipTest('current uid has no passwd entry')
        with tempfile.TemporaryDirectory() as directory:
            bus_path = directory + '/bus'
            proxy_path = directory + '/proxy'
            config = Path(directory) / 'bus.conf'
            config.write_text('''<busconfig>
              <type>session</type>
              <listen>unix:tmpdir=/tmp</listen>
              <auth>EXTERNAL</auth>
              <policy context="default">
                <allow send_destination="*"/>
                <allow receive_sender="*"/>
                <allow own="*"/>
              </policy>
            </busconfig>''')
            daemon = subprocess.Popen(
                ['dbus-daemon', '--config-file=' + str(config), '--nofork',
                 '--print-address=1', '--address=unix:path=' + bus_path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                address = daemon.stdout.readline()
                if not address:
                    _, error = daemon.communicate(timeout=5)
                    if 'Operation not permitted' in error or 'Permission denied' in error:
                        self.skipTest('environment forbids private sockets: ' + error.strip())
                    self.fail(error)
                self.assertTrue(address.startswith('unix:'))
                with socket.socket(socket.AF_UNIX) as listener:
                    listener.bind(proxy_path)
                    listener.listen(1)
                    listener.settimeout(5)
                    errors = queue.Queue()

                    def run():
                        try:
                            front, _ = listener.accept()
                            with front, socket.socket(socket.AF_UNIX) as back:
                                front.settimeout(5)
                                back.settimeout(5)
                                back.connect(bus_path)
                                bridge(front, back)
                        except Exception as error:
                            errors.put(error)

                    thread = threading.Thread(target=run, daemon=True)
                    thread.start()
                    yield proxy_path
                    thread.join(6)
                    self.assertFalse(thread.is_alive())
                    self.assertTrue(errors.empty(), list(errors.queue))
            finally:
                if daemon.poll() is None:
                    daemon.terminate()
                daemon.communicate(timeout=5)

    def test_private_bus_hello_and_method_call(self):
        with self.private_bus() as proxy_path:
            result = subprocess.run(
                ['dbus-send', '--bus=unix:path=' + proxy_path, '--print-reply',
                 '--dest=org.freedesktop.DBus', '/org/freedesktop/DBus',
                 'org.freedesktop.DBus.ListNames'],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('org.freedesktop.DBus', result.stdout)

    def test_real_descriptor_call_gets_error_and_connection_survives(self):
        def reply_to(app, serial):
            while True:
                frame = protocol.read_frame(app)
                self.assertIsNotNone(frame)
                if frame.message_type in (2, 3) and frame.fields[5] == serial:
                    return frame

        with self.private_bus() as proxy_path, socket.socket(socket.AF_UNIX) as app:
            app.settimeout(5)
            app.connect(proxy_path)
            app.sendall(b'\0AUTH EXTERNAL %s\r\n' % str(os.getuid()).encode().hex().encode())
            self.assertTrue(app.recv(4096).startswith(b'OK '))
            app.sendall(b'NEGOTIATE_UNIX_FD\r\n')
            self.assertEqual(app.recv(4096), b'AGREE_UNIX_FD\r\n')
            app.sendall(b'BEGIN\r\n' + message(serial=1, fields={**BUS, 3: ('s', 'Hello')}))
            self.assertEqual(reply_to(app, 1).message_type, 2)

            read_end, write_end = os.pipe()
            self.addCleanup(os.close, read_end)
            call = message(serial=2, fields={**BUS, 3: ('s', 'GetConnectionUnixUser'),
                                             8: ('g', 'h'), 9: ('u', 1)},
                           body=struct.pack('<I', 0))
            app.sendmsg([call], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                  array.array('i', [write_end]).tobytes())])
            os.close(write_end)
            error = reply_to(app, 2)
            self.assertEqual(error.message_type, 3)
            self.assertEqual(error.fields[4], 'org.freedesktop.DBus.Error.NotSupported')
            # EOF proves the adapter kept no copy of the received write end.
            self.assertEqual(select.select([read_end], [], [], 2)[0], [read_end])
            self.assertEqual(os.read(read_end, 1), b'')

            app.sendall(message(serial=3, fields={**BUS, 3: ('s', 'ListNames')}))
            self.assertEqual(reply_to(app, 3).message_type, 2)


if __name__ == '__main__':
    unittest.main()

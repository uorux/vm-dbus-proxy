"""Optional real daemon smoke test. No session bus or host services are touched."""
import queue
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from vm_dbus_proxy.proxy import bridge


@unittest.skipUnless(shutil.which('dbus-daemon') and shutil.which('dbus-send'),
                     'requires dbus-daemon and dbus-send')
class PrivateBusTests(unittest.TestCase):
    def test_private_bus_hello_and_method_call(self):
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
                    result = subprocess.run(
                        ['dbus-send', '--bus=unix:path=' + proxy_path, '--print-reply',
                         '--dest=org.freedesktop.DBus', '/org/freedesktop/DBus',
                         'org.freedesktop.DBus.ListNames'],
                        capture_output=True, text=True, timeout=10)
                    thread.join(6)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('org.freedesktop.DBus', result.stdout)
                    self.assertTrue(errors.empty(), list(errors.queue))
            finally:
                if daemon.poll() is None:
                    daemon.terminate()
                daemon.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()

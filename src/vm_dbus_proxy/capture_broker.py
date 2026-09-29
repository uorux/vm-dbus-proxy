"""Per-VM capture worker supervisor. Unix descriptors never cross this broker's host.

Run as the dedicated capture user, not as the desktop/application or GPU backend.
The authenticated adapter owns the lifetime of each broker connection.
"""
import argparse
import array
import errno
import json
import logging
import os
import selectors
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
from contextlib import ExitStack

LOG = logging.getLogger(__name__)
MAX_SESSIONS = 4
MAX_REQUEST = 4096
# A private PipeWire per approved share: the protocol server, client nodes
# (the publisher and the app's consumer are both streams), links and metadata
# for WirePlumber. No factory that loads SPA plugins by client-chosen name
# (spa-node-factory, adapter: both honour `library.name`), no device monitors,
# no host socket. The app gets an unrestricted client of this instance only
# (per socket: the deprecated access.force was ignored, falling back to
# flatpak detection).
PIPEWIRE_CONF = '''context.properties = { core.daemon = true core.name = pipewire-0 }
context.spa-libs = { support.* = support/libspa-support }
context.modules = [
 { name = libpipewire-module-protocol-native }
 { name = libpipewire-module-client-node }
 { name = libpipewire-module-link-factory }
 { name = libpipewire-module-metadata }
 { name = libpipewire-module-access args = { access.socket = { pipewire-0 = unrestricted pipewire-0-manager = unrestricted } } }
]
'''
WIREPLUMBER_CONF = '''wireplumber.profiles = {
 capture = {
  inherits = [ policy, mixin.systemwide-session, mixin.stateless ]
  support.dbus = disabled
  support.system-dbus = disabled
 }
}
'''


def receive_request(conn):
    fds = []
    try:
        data, ancillary, flags, _ = conn.recvmsg(MAX_REQUEST, socket.CMSG_SPACE(8 * 4), socket.MSG_CMSG_CLOEXEC)
        unexpected = False
        for level, kind, payload in ancillary:
            if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                unexpected = True
                continue
            values = array.array('i')
            values.frombytes(payload[:len(payload) - len(payload) % values.itemsize])
            fds.extend(values)
        if unexpected:
            raise ValueError('unexpected ancillary data')
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            raise ValueError('truncated broker request')
        request = json.loads(data)
        if not isinstance(request, dict):
            raise ValueError('request must be an object')
        return request, fds
    except BaseException:
        for fd in fds:
            os.close(fd)
        raise


def send_reply(conn, value, fd=None):
    rights = [] if fd is None else [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [fd]))]
    conn.sendmsg([json.dumps(value).encode()], rights)


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def start(stack, command, *, fds=(), env=None):
    process = subprocess.Popen(command, pass_fds=fds, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL)
    stack.callback(stop, process)
    return process


def wait_socket(path, process, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('private PipeWire exited during startup')
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.connect(path)
            return connection
        except (FileNotFoundError, ConnectionRefusedError):
            connection.close()
            time.sleep(0.05)
    raise TimeoutError('private PipeWire startup timed out')


def read_status(fd, process, timeout=15):
    deadline = time.monotonic() + timeout
    data = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while len(data) < MAX_REQUEST:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise TimeoutError('capture publisher startup timed out')
            chunk = os.read(fd, 1)
            if not chunk:
                raise RuntimeError('capture publisher exited before announcing a node')
            data += chunk
            if chunk == b'\n':
                result = json.loads(data)
                node = result.get('node')
                if isinstance(node, bool) or not isinstance(node, int) or not 0 < node < 0xffffffff:
                    raise ValueError('invalid guest PipeWire node')
                return node
    raise ValueError('oversized capture publisher status')


def serve_session(conn, args):
    with conn, ExitStack() as stack:
        conn.settimeout(15)
        _, uid, _ = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != args.allowed_uid:
            raise PermissionError('capture broker peer is not the configured adapter user')
        request, fds = receive_request(conn)
        for fd in fds:
            stack.callback(os.close, fd)
        role = request.get('role')
        if role != args.role or len(fds) != (2 if role == 'host' else 1):
            raise ValueError('invalid broker role or descriptor count')
        processes = []
        if role == 'host':
            node = request.get('node')
            if isinstance(node, bool) or not isinstance(node, int) or not 0 < node < 0xffffffff:
                raise ValueError('invalid host PipeWire node')
            processes.append(start(stack, [args.worker, 'host', '--pipewire-fd', str(fds[0]),
                '--control-fd', str(fds[1]), '--node-id', str(node), '--inject-socket', args.inject_socket], fds=fds))
            send_reply(conn, {'ok': True})
        else:
            directory = tempfile.mkdtemp(prefix='session-', dir=args.runtime_dir)
            stack.callback(shutil.rmtree, directory, ignore_errors=True)
            env = dict(os.environ, XDG_RUNTIME_DIR=directory, PIPEWIRE_RUNTIME_DIR=directory,
                       PIPEWIRE_REMOTE='pipewire-0', XDG_CONFIG_HOME=directory,
                       XDG_STATE_HOME=directory, XDG_CACHE_HOME=directory)
            config = os.path.join(directory, 'pipewire.conf')
            with open(config, 'w') as out:
                out.write(PIPEWIRE_CONF)
            pipewire = start(stack, [args.pipewire, '-c', config], env=env)
            processes.append(pipewire)
            application = wait_socket(os.path.join(directory, 'pipewire-0'), pipewire)
            stack.callback(application.close)
            os.makedirs(os.path.join(directory, 'wireplumber', 'wireplumber.conf.d'))
            with open(os.path.join(directory, 'wireplumber', 'wireplumber.conf.d', 'capture.conf'), 'w') as out:
                out.write(WIREPLUMBER_CONF)
            processes.append(start(stack, [args.wireplumber, '--profile', 'capture'], env=env))
            readfd, writefd = os.pipe2(os.O_CLOEXEC)
            stack.callback(os.close, readfd)
            command = [args.worker, 'guest', '--control-fd', str(fds[0]),
                       '--status-fd', str(writefd), '--render-node', args.render_node,
                       '--capture-device', args.capture_device]
            inherited = [fds[0], writefd]
            if args.capture_fd is not None:
                # Only the publisher gets the node; PipeWire and WirePlumber
                # (close_fds) do not.
                command += ['--capture-fd', str(args.capture_fd)]
                inherited.append(args.capture_fd)
            try:
                publisher = start(stack, command, fds=inherited, env=env)
            finally:
                os.close(writefd)
            processes.append(publisher)
            node = read_status(readfd, publisher)
            send_reply(conn, {'node': node}, application.fileno())
        conn.settimeout(1)
        while all(process.poll() is None for process in processes):
            try:
                data = conn.recv(1)
                if not data:
                    LOG.info('%s capture adapter closed its broker connection', role)
                    return
                raise ValueError('unexpected broker control data')
            except socket.timeout:
                pass
        for process in processes:
            code = process.poll()
            if code is not None:
                LOG.warning('%s capture worker %s exited with status %s',
                            role, os.path.basename(process.args[0]), code)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role', choices=['host', 'guest'], required=True)
    parser.add_argument('--listen', required=True)
    parser.add_argument('--allowed-uid', type=int, required=True)
    parser.add_argument('--worker', required=True)
    parser.add_argument('--inject-socket')
    parser.add_argument('--runtime-dir')
    parser.add_argument('--pipewire')
    parser.add_argument('--wireplumber')
    parser.add_argument('--render-node', default='/dev/dri/renderD128')
    parser.add_argument('--capture-device', default='/dev/nvgpu-capture')
    parser.add_argument('--capture-fd', type=int,
                        help='an inherited open /dev/nvgpu-capture (systemd OpenFile=), passed to publishers only')
    args = parser.parse_args()
    if args.role == 'host' and not args.inject_socket:
        parser.error('--inject-socket is required on the host')
    if args.role == 'guest' and (not args.runtime_dir or not args.pipewire or not args.wireplumber):
        parser.error('--runtime-dir and --pipewire are required in the guest')
    logging.basicConfig(level=logging.INFO)
    slots = threading.BoundedSemaphore(MAX_SESSIONS)
    def handle(conn):
        try:
            serve_session(conn, args)
        except (OSError, ValueError, RuntimeError) as exc:
            LOG.warning('capture session stopped: %s', exc)
        finally:
            conn.close()
            slots.release()
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
        listener.bind(args.listen)
        # The parent directory is owned by this service; SO_PEERCRED above
        # authenticates the one adapter uid before any descriptors are used.
        os.chmod(args.listen, 0o666)
        listener.listen(8)
        while True:
            try:
                conn, _ = listener.accept()
            except OSError as exc:
                LOG.warning('accept failed: %s', exc)
                if exc.errno in (errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.ENOMEM):
                    time.sleep(0.1)
                continue
            if slots.acquire(blocking=False):
                threading.Thread(target=handle, args=(conn,), daemon=True).start()
            else:
                conn.close()

if __name__ == '__main__':
    main()

# vm-dbus-proxy

A small, message-aware D-Bus adapter for applications inside Linux VMs. Python
3.11 or newer, standard library only.

```
host session bus ↔ host filtering proxy ↔ FD-free VM transport
                                          ↕
                                   vm-dbus-proxy ↔ app
```

The host filtering proxy (for example, xdg-dbus-proxy) remains responsible for
access policy. Each application connection gets its own upstream connection:
Hello, unique names, name ownership, subscriptions, replies and ordinary signals
pass through unchanged. Notifications and portal calls can therefore use the
host services when host policy permits them.

## Why an adapter?

A byte-stream relay cannot carry Unix file descriptors across a VM boundary.
Some clients mishandle that transport limitation and crash when attempting an
FD-bearing call. This adapter negotiates descriptor receipt on the guest side,
closes received descriptors, and returns
`org.freedesktop.DBus.Error.NotSupported` for calls requiring a reply. Ordinary
messages continue on the same connection. FD-bearing replies become errors to
the original caller; FD-bearing signals and no-reply calls are dropped.

The plain adapter does **not** implement cross-VM descriptor transfer. Its
upstream transport must never negotiate Unix FD support. The optional
ScreenCast bridge described below handles approved capture streams through
virtio-nvgpu; other descriptor-bearing portals remain unsupported.

## Run

```sh
PYTHONPATH=src python -m vm_dbus_proxy \
  --listen /run/sbx/bus/bus \
  --upstream /run/sbx/bus/transport.sock
```

Set the application's `DBUS_SESSION_BUS_ADDRESS` to
`unix:path=/run/sbx/bus/bus`. The upstream socket must connect to the filtered
host bus, not an unrestricted host session bus. The listener is mode 0600.
Use a supervisor to remove a stale listener socket before restarting; the
program deliberately refuses to overwrite an existing path.

`default.nix` exports a runnable package when imported with `{ inherit pkgs; }`.
`pyproject.toml` also supports installation using standard Python packaging.

## Limits and trust

This is a compatibility adapter inside the guest, not a host security boundary.
A compromised guest can bypass it, so filtering and FD-negotiation restrictions
must also be enforced on the host. Authentication is passed to the existing
host transport; the adapter does not invent identities or grant permissions.

Frames default to at most 32 MiB, headers to 1 MiB, descriptors to 256 per frame,
and concurrent clients to 128. `--max-message-bytes` adjusts the frame cap up to
128 MiB. Oversized or malformed traffic disconnects that client. Buffers are
bounded per connection; these limits are not a total memory budget.

## Tests

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
```

Tests cover framing, descriptor cleanup, authentication, transparent forwarding,
and unsupported-call responses. The private dbus-daemon integration test needs
Unix socket permissions and skips when the environment prohibits sockets. CI
runs the suite on Linux with dbus installed. A passing unit suite does not
substitute for a Chromium launch and host-portal test in the actual VM.

## NixOS integration in the parent checkout

The parent configuration pins this repository through a local Git flake input
until it can be published. After changing proxy code, commit this repository
and run `nix flake update vm-dbus-proxy` in the parent checkout. The guest starts
`sbx-dbus-proxy.service` after `sbx-relay.service`; application launch waits for
its socket. Inspect `journalctl -u sbx-dbus-proxy` inside the VM when testing.

## VM ScreenCast bridge

Capture-enabled virtio-nvgpu VMs use `vm-dbus-capture-proxy` on both sides of
an explicitly framed `capture-dbus` relay. The host adapter negotiates FD
support only with the local filtering proxy. After an approved ScreenCast
Start, it obtains the restricted PipeWire remote on that same bus connection.
A dedicated per-VM broker consumes it and injects the approved DMA-BUFs.
The guest broker imports them and starts a private PipeWire server and
WirePlumber policy manager for that session. The adapter replaces the portal
node ID and returns a connected guest-local PipeWire FD to the application.
No host PipeWire socket or arbitrary descriptors cross the VM boundary.

The host capture adapter is part of the host boundary: it validates the
portal response sender and only accepts a PipeWire FD correlated to its own
OpenPipeWireRemote request. The injector authenticates the separate capture
broker uid. Each broker authenticates its configured adapter uid, limits
concurrent sessions to four and terminates workers when the adapter closes.
Other FD-bearing operations retain the ordinary unsupported-operation policy.
See [capture/README.md](capture/README.md) for format and lifecycle limits.
Python entry points are available via pip; the C worker requires the Nix
package or a separate native build.

After rebuilding the parent NixOS configuration, fully stop and relaunch the
VM, then test:

1. Share one monitor in Chromium; verify moving content for at least 30 seconds.
2. Stop, share one window, stop, and share again. Cancel the picker once too.
3. Close the browser/VM during a share; the host sharing indicator must disappear.
4. Check notifications and file selection before and after sharing.

Capture currently requires virtio-nvgpu and an approved DMA-BUF stream. There
is no CPU-copy fallback. Run-time GPU/PipeWire acceptance is not covered by
unit tests. Collect these logs after a failed attempt:

```sh
# Host
journalctl -b -u sandbox-vm-ungoogled-chromium-capture-bus \
  -u sandbox-vm-ungoogled-chromium-capture-broker \
  -u sandbox-vm-ungoogled-chromium-gpu --since '-5 minutes' --no-pager
# Inside the VM, using its existing SSH entry point
journalctl -b -u sbx-dbus-proxy -u sbx-capture-broker --no-pager
```

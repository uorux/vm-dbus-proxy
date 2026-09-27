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

This does **not** implement cross-VM descriptor transfer. Portals returning
PipeWire sockets or other descriptors still need a dedicated bridge. The
upstream transport must never negotiate Unix FD support; it should enforce
that independently of this adapter. No access policies are relaxed here.

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

# Screen frame bridge

`vm-capture host` consumes a portal-restricted PipeWire remote descriptor,
imports its GPU buffers into the VM's virtio-nvgpu injection socket, and
announces frames on a private control channel. `vm-capture guest` opens those
imports through `/dev/nvgpu-capture` and publishes them as a PipeWire video
source. The broker creates a private PipeWire daemon and policy manager per
session; its connected client socket and source node are returned to the app.

```
vm-capture host --control-fd FD --pipewire-fd FD --node-id ID --inject-socket PATH
vm-capture guest --control-fd FD --status-fd FD
```

Both accept `--render-node PATH` (default `/dev/dri/renderD128`); guest accepts
`--capture-device PATH`. The guest uses `XDG_RUNTIME_DIR` and `PIPEWIRE_REMOTE`
to find its private daemon and writes `{"node":123}\n` to the status pipe.
Descriptors, including the control channel, are inherited explicitly by the
broker. No buffer token appears in arguments, environment or logs. Host runs
as the VM's distinct injection uid. Guest runs as a capture account, not the
app user. Closing either side ends the session and releases the imports.

The control protocol is 64-byte little-endian records: magic `0x31504356`,
operation, slot, id, width, height, SPA format, count (eight u32s), modifier and
sequence (two u64s), 16-byte token. Sequence is monotonically increasing. FRAME reuses id/count as crop x/y,
width/height as crop size, and format as the SPA video transform (0–7).
Guest timestamps are stamped on arrival rather than using host monotonic time.
Operations: CONFIG=1, BUFFER=2, READY=3, FRAME=4, DONE=5. CONFIG precedes up to
32 BUFFER records, then READY. Each FRAME holds its original host PipeWire
buffer until the matching DONE arrives after the guest consumer returns it.
A five-second missing acknowledgement terminates the session rather than
reusing a buffer still owned by a consumer. The channel carries metadata,
not pixels; imported DMA-BUFs carry pixels.

Current scope: one packed BGRA/BGRx video stream, at most 8192x8192, one plane
per buffer. Host EGL modifier discovery uses the configured render device,
with a linear-only fallback if discovery fails. The backend verifies GPU
ownership, layout and import quotas. Cursor should be embedded by the portal
(the helper does not transport separate cursor metadata). Explicit-sync
metadata, buffer-pool reallocation and format changes fail the session with
a diagnostic; retry sharing after changing monitor layout. No software-copy
fallback, audio transport or arbitrary PipeWire protocol forwarding exists.

Build with `default.nix`; `checkPhase` compiles and runs control decoder tests
without GPU devices or a daemon. Hardware acceptance still requires selecting
a host monitor, observing changing frames in Chromium, then stopping, retrying,
and closing the VM while a share is active.

`nvgpu_capture.h` is copied verbatim from virtio-nvgpu's dual-licensed UAPI,
with its license notice retained.

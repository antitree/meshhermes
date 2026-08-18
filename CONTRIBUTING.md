# Contributing to MeshHermes

Thanks for taking a look. This document covers how to get a working dev
setup, the conventions the codebase follows, and the few non-obvious traps
that will otherwise cost you an afternoon.

## Development setup

```bash
git clone <your-fork> meshhermes
cd meshhermes
pip install -e ".[dev]"
pytest
```

**The full test suite runs with no radio and no Hermes install.** That is a
hard requirement, not a nicety — see [Testing](#testing).

## The non-obvious bits

These caught the original author; they will catch you too.

### The plugin loads as a package rooted at the plugin directory

Hermes imports `<plugin_dir>/__init__.py` with `__path__` set to the plugin
directory, then calls `register(ctx)`. That means:

- Modules live at the **repo root**, not in a `src/meshhermes/` package.
- Sibling modules are **relative imports** (`from . import transport`).
- Every module also carries a top-level import fallback, so it stays
  importable when there is no parent package (tests, linting, `python -c`).
  Keep that pattern if you add a module.

### Do not name a module `tools.py`

It shadows Hermes' own `tools` package and breaks imports in confusing
ways. That is why the tool handlers live in `mesh_tools.py`.

### `Platform("meshtastic")` raises unless the platform is registered

The Hermes `Platform` enum only mints a member for a platform that is
already in `platform_registry` or bundled in-tree. `tests/conftest.py`
registers it up front to reproduce the real precondition.

### The library is threaded pubsub; Hermes is asyncio

`meshtastic` delivers events on a background receive thread. Never call a
coroutine from a pubsub callback — bridge with
`asyncio.run_coroutine_threadsafe` (see `transport.bridge_to_loop`). Every
blocking library call (`SerialInterface(...)`, `sendText`) goes through
`asyncio.to_thread`. `pub.subscribe` is global, so callbacks must filter on
interface identity and unsubscribe on disconnect.

### Do not add a reconnect loop

Hermes' `GatewayRunner._platform_reconnect_watcher` owns retry with its own
backoff. A second loop inside the adapter means two loops racing for one
serial port. Report state via `_mark_connected()` / `_mark_disconnected()` /
`_set_fatal_error()` and let the gateway retry.

### Do not reimplement access control

`dm_policy` and `group_policy` are exposed as attributes for Hermes'
authorization layer to enforce. Adding allowlist or pairing checks here
double-gates and diverges from every other Hermes platform.

## Testing

```bash
pytest                    # everything, no hardware needed
pytest tests/test_e2e.py  # the full inbound/outbound path
```

The suite drives a `FakeMeshInterface` that publishes on the **real** pubsub
topics, so the adapter's genuine subscription wiring and thread bridge are
exercised rather than mocked away.

**A fake is only as good as its fidelity to the real device.** Three
shipped bugs passed the entire suite while being broken on hardware,
because the fake mirrored a shape the library does not actually use — for
example, on a real radio `iface.localConfig` and `iface.channels` are both
`None` (the data lives under `iface.localNode`). When you touch anything
that reads device state, add a fixture matching the real protobuf layout
(see `TestRealDeviceShapes`) and, if you can, verify against hardware.

### Hardware testing

```bash
python scripts/smoke_hardware.py --transport serial --port /dev/ttyUSB0
```

Never runs in CI. Two things commonly steal the serial port and produce
confusing intermittent failures:

- another process already using the radio (a Meshtastic CLI, an MQTT
  bridge, another container) — a serial port cannot be shared;
- **ModemManager**, which probes CDC-ACM devices on Linux. Fix with a udev
  rule setting `ENV{ID_MM_DEVICE_IGNORE}="1"` for your radio's vendor ID.

> Transmitting requires a correctly-set LoRa region and compliance with your
> local RF regulations.

## Conventions

- **Comments explain why, not what.** Several constants here look arbitrary
  and are not (the 1.5s inter-chunk delay, the 230-byte frame ceiling, the
  decimal/hex disambiguation rule). If you change one, update the reasoning.
- **Chunking is measured in UTF-8 bytes, never characters.** A LoRa frame is
  bytes; measuring characters overflows on emoji and CJK.
- **Tool handlers return `json.dumps(...)` strings and never raise.**
- **Never set the LoRa region and never rename the device.** A partial
  `setConfig` can zero `tx_enabled`; `setOwner()` reboots the radio.
- **Never log GPS positions.** They are the coordinates of real people.

## Pull requests

1. Branch from `main`.
2. Add tests — a bug fix should come with a test that fails without it.
3. Keep `pytest` green with no hardware attached.
4. Describe *why* in the commit message; the diff already shows what.
5. Note explicitly if you verified against a real radio, and which model.

## Security

Please do not open a public issue for a security problem — see
[SECURITY.md](SECURITY.md).

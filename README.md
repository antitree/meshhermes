# MeshHermes

**Talk to your AI agent over a LoRa radio — off-grid, no internet, no cell service.**

MeshHermes connects a [Meshtastic](https://meshtastic.org) mesh radio to
[Hermes Agent](https://github.com/NousResearch/hermes-agent). People on the mesh
send text over the air; the agent reads it, thinks, and replies over the air.

Ask it a question from a trailhead. Have it watch a channel and answer when
addressed. Query mesh telemetry — who is online, what their battery looks like,
how far away they are — without touching a browser.

Installs into `~/.hermes/plugins/` and needs **zero changes to Hermes core**.

---

## Requirements

- Hermes Agent with plugin support
- Python 3.10+
- A Meshtastic radio connected by **USB** or reachable over **WiFi**
- A correctly-set LoRa region (`meshtastic --set lora.region US`)

## Install

```bash
hermes plugins install antitree/meshhermes
pip install meshtastic
```

`hermes plugins install` clones the repo into `~/.hermes/plugins/` and offers to
enable it. Third-party platform plugins are opt-in, so it stays off until you
say yes (or pass `--enable`).

<details>
<summary>Manual install</summary>

```bash
git clone https://github.com/antitree/meshhermes \
  ~/.hermes/plugins/meshtastic-platform
pip install meshtastic
```

Then add it to `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - meshtastic-platform
```
</details>

## Configure

Run the wizard, or write the config by hand:

```bash
hermes gateway setup      # choose Meshtastic
hermes gateway restart
```

### Minimal working config

```yaml
platforms:
  meshtastic:
    enabled: true
    extra:
      transport: serial
      serial_port: /dev/ttyUSB0   # blank = autodetect a single radio
```

### Environment variables

Environment variables take precedence over `config.yaml`.

| Variable | Required | Notes |
|---|---|---|
| `MESHTASTIC_TRANSPORT` | yes | `serial` or `tcp` |
| `MESHTASTIC_SERIAL_PORT` | serial only | e.g. `/dev/ttyUSB0`. Blank autodetects a single attached radio. |
| `MESHTASTIC_TCP_HOST` | tcp only | hostname/IP, e.g. `meshtastic.local` |
| `MESHTASTIC_NODE_NAME` | no | mention trigger; defaults to the device's `longName` |
| `MESHTASTIC_ALLOWED_USERS` | no | comma-separated `!hex` node IDs |
| `MESHTASTIC_ALLOW_ALL_USERS` | no | dev only — opens the bot to every node in range, **inbound and outbound** |
| `MESHTASTIC_HOME_CHANNEL` | no | cron/notification delivery target |
| `MESHTASTIC_EXPOSE_POSITION` | no | default `true`; `false` hides GPS in tool output |
| `MESHTASTIC_AUTO_INSTALL` | no | default `false`; `true` pip-installs `meshtastic` on connect if missing |

### Transports

**Serial (USB)** — the radio is plugged into the machine running Hermes.

**TCP (WiFi)** — the radio is reachable over the network by hostname or IP.

> A serial port cannot be shared. If another program already holds your radio
> (the Meshtastic CLI, an MQTT bridge, another container), Hermes cannot open
> it. On Linux, ModemManager also probes USB radios — see
> [Troubleshooting](#troubleshooting).

BLE and MQTT are **not supported yet**. Configuring them fails at validation
with a clear error rather than silently doing nothing — see [ROADMAP.md](ROADMAP.md).

---

## Examples

### Monitoring LongFast and answering when addressed

The common setup: the agent sits on a public channel, stays quiet, and responds
only when someone addresses it by name.

```yaml
platforms:
  meshtastic:
    enabled: true
    extra:
      transport: serial
      serial_port: /dev/ttyUSB0
      node_name: Hermes            # the name people address on the channel

      group_policy: allowlist      # only channels listed below are handled
      dm_policy: pairing           # DMs require pairing approval

      channels:
        LongFast:
          require_mention: true    # ignore chatter; answer only when addressed

      text_chunk_limit: 200        # bytes per LoRa frame
      chunk_delay_seconds: 1.5     # pacing between frames
```

On the mesh, someone types into the LongFast channel:

```
@Hermes what's the weather looking like for tomorrow?
```

Hermes wakes, answers, and the reply comes back over the air — split across as
many frames as it needs, plain text, no markdown:

```
Hermes: Rain likely tomorrow AM, clearing by
afternoon. High 61F, wind 10-15mph from SW.
```

Messages that don't address it are ignored entirely. On a shared channel that
matters: every reply costs airtime that everyone else is also using.

<!-- Screenshot slot: Meshtastic Android app showing a LongFast exchange with Hermes. -->

### Talking to it privately

With `dm_policy: pairing`, a new node's first DM creates a pairing request you
approve from the Hermes side. Once approved, DMs need no mention — addressing is
implicit:

```
you  ▸ how many nodes are online right now?
     ◂ 9 nodes heard in the last hour. Closest:
       Alice Radio, 1 hop, SNR 6.2. Furthest:
       repeater-north, 3 hops.
```

To skip pairing during testing, use an explicit allowlist instead:

```yaml
      dm_policy: allowlist
      allow_from: ["!aabbccdd"]    # must be a list — allow_from: "*" is rejected
```

### Asking about the mesh itself

The four tools are available in any Hermes session — terminal, Discord, or over
the radio itself:

```
> which nodes have low batteries?

  mesh_nodes → 9 nodes
  Two below 30%: tomagatchi (22%, 3.6V) and
  repeater-north (28%, 3.7V). Everything else
  is above 60%.
```

### Sending a scheduled report to the mesh

Set a home channel and cron jobs can deliver to it:

```bash
MESHTASTIC_HOME_CHANNEL=channel:LongFast
```

```
> every morning at 7am, send the day's forecast to the mesh

  Created cron job "morning-forecast", delivering to meshtastic.
```

---

## Access control

Access control is enforced by Hermes itself, not reimplemented here. The
adapter exposes `dm_policy` and `group_policy`; the gateway's authorization
layer (including its real pairing store) applies them.

| Policy | Values | Meaning |
|---|---|---|
| `dm_policy` | `open`, `pairing`, `allowlist`, `disabled` | who may DM the bot |
| `group_policy` | `open`, `allowlist`, `disabled` | whether channel messages are handled |

`dm_policy: open` additionally requires `allow_from` to explicitly contain
`"*"`. Opening a radio to everyone in range should never happen through a
config typo.

**Mention gating.** On a channel, the bot ignores messages that do not address
it (`@Hermes ...`, `Hermes: ...`, `Hermes, ...`). This defaults to **on** —
replying to everything on a shared channel wastes airtime everyone shares. Set
`require_mention: false` per channel to change it. DMs never require a mention.

> Meshtastic node IDs are **not authenticated** and channel traffic is readable
> by anyone holding the channel key. An allowlist raises the bar; it is not an
> identity guarantee. See [SECURITY.md](SECURITY.md).

## Tools

The plugin registers four agent-callable tools.

| Tool | Access | Returns |
|---|---|---|
| `mesh_nodes` | read-only | known nodes: ID, names, hops, SNR/RSSI, battery, last heard, position |
| `mesh_telemetry` | read-only | battery, voltage, channel utilization, air-util-TX, temp/humidity/pressure, with recent history |
| `mesh_channels` | read-only | configured channels: index, name, primary |
| `mesh_send` | **gated** | transmit a message to a node or channel |

`mesh_send` is gated by `dm_policy`/`group_policy` and rate-limited (5 sends per
60s). Airtime is a shared, legally regulated resource, and the agent must not be
able to flood it. The same gate applies to **every** outbound path — gateway
replies, the tool, and cron delivery — so policy cannot be sidestepped by
routing through a different tool.

### Position privacy

Node positions are the GPS coordinates of real people. MeshHermes:

- **never** logs positions,
- rounds coordinates to 4 decimal places (≈11 m) in tool output, configurable
  via `position_precision` and clamped so it cannot be set finer,
- suppresses them entirely with `MESHTASTIC_EXPOSE_POSITION=false`.

## Hardware smoke test

Not run in CI — it needs a radio and it transmits.

```bash
python scripts/smoke_hardware.py --transport serial --port /dev/ttyUSB0
python scripts/smoke_hardware.py --transport tcp --host meshtastic.local
```

It connects, reads device info, checks the region, lists nodes, verifies
chunking by transmitting a >200-byte message to itself, waits for an inbound DM
from another node, then disconnects and confirms the port was released.

> **Transmitting requires a correctly-set LoRa region and compliance with your
> local RF regulations.**

## Troubleshooting

**`Timed out waiting for connection completion`** — something else holds the
serial port. A port can have exactly one owner. Stop the other program, or give
Hermes its own radio.

**Intermittent connection failures on Linux** — ModemManager probes USB radios
and steals the port during the Meshtastic handshake:

```bash
sudo tee /etc/udev/rules.d/99-meshtastic-no-mm.rules >/dev/null <<'RULE'
SUBSYSTEM=="tty", ATTRS{idVendor}=="239a", ENV{ID_MM_DEVICE_IGNORE}="1"
RULE
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=tty
```

Replace `239a` with your radio's USB vendor ID (`lsusb`).

**`LoRa region is UNSET`** — MeshHermes refuses to transmit until you set it.
This plugin never sets the region for you; a partial `setConfig` can zero
`tx_enabled` and silently disable transmission:

```bash
meshtastic --set lora.region US
```

**The bot ignores channel messages** — mention gating is on by default. Address
it by name, or set `require_mention: false` for that channel.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

313 tests, passing on Python 3.10–3.13 with **no radio and no Hermes install
required**. A fake interface publishes on the real pubsub topics, so the
adapter's actual subscription wiring and its thread→asyncio bridge are
exercised rather than mocked away.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup and the
non-obvious traps.

## Deliberate differences from MeshClaw

| Behaviour | MeshHermes | Why |
|---|---|---|
| **Chunking unit** | UTF-8 **bytes** | MeshClaw measures UTF-16 code units against a byte-sized LoRa frame, so emoji and CJK overflow it. This is a bug fix. |
| **Device rename (`setOwner`)** | not done | Renaming reboots the radio and forces a ~30s reconnect — hostile on a radio you may share. Use the `meshtastic` CLI. |
| **LoRa region** | read, never set | A partial `setConfig` can zero `tx_enabled`. Choosing a region for the user is also a legal hazard. |
| **Reconnection** | gateway-owned | Hermes already runs a reconnect watcher with backoff. A second loop inside the adapter would race it for the serial port. |
| **Access control** | Hermes-native | Uses Hermes' policies and real pairing store instead of a parallel implementation. |
| **Multi-account** | Hermes profiles | Replaces MeshClaw's `accounts.ts`. |
| **Telemetry tools** | implemented | Listed as unbuilt roadmap in MeshClaw. |

## Acknowledgements

**Inspired by [MeshClaw](https://github.com/Seeed-Solution/MeshClaw)** by
[Seeed Studio](https://www.seeedstudio.com/) — the Meshtastic channel plugin for
OpenClaw. MeshClaw worked out the hard parts of bridging a LoRa mesh to an AI
agent: node-ID normalization, LoRa-sized chunking, channel and mention policy.
MeshHermes is an independent Python implementation of those ideas for Hermes
Agent, and its normalization, chunking, and policy modules follow MeshClaw's
design closely. MeshClaw is MIT licensed.

Thanks also to:

- **[Meshtastic](https://meshtastic.org)** — the open-source LoRa mesh firmware
  and the [`meshtastic` Python library](https://github.com/meshtastic/python)
  this plugin is built on. GPL-3.0.
- **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** by
  [Nous Research](https://nousresearch.com/) — the agent framework, whose plugin
  interface made this possible without touching core. MIT licensed.

## License

MIT — see [LICENSE](LICENSE).

MeshHermes is an independent project. It is not affiliated with or endorsed by
Meshtastic, Seeed Studio, or Nous Research.

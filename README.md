# MeshHermes

A [Meshtastic](https://meshtastic.org) LoRa mesh platform plugin for
[Hermes Agent](https://github.com/NousResearch/hermes-agent).

Talk to your Hermes agent over a LoRa radio — off-grid, no internet, no cell
service. Radio users send text over the mesh; the agent replies over the mesh.
Ships with read-only mesh telemetry tools so the agent can answer questions
about your network.

Installs into `~/.hermes/plugins/` and requires **zero changes to Hermes core**.

---

## Requirements

- Hermes Agent with plugin support
- Python 3.10+
- A Meshtastic radio, connected by **USB** or reachable over **WiFi**
- `pip install meshtastic`

## Install

```bash
hermes plugins install antitree/meshhermes
pip install meshtastic
```

`hermes plugins install` clones the repo into `~/.hermes/plugins/` and offers
to enable it. Third-party platform plugins are opt-in, so it stays disabled
until you say yes (or pass `--enable`).

<details>
<summary>Manual install</summary>

```bash
git clone https://github.com/antitree/meshhermes \
  ~/.hermes/plugins/meshtastic-platform
pip install meshtastic
```
</details>

Enable it in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - meshtastic-platform
```

Then run the setup wizard, or configure by hand (below):

```bash
hermes gateway setup     # choose Meshtastic
hermes gateway restart
```

## Configure

### Environment variables

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

### `config.yaml`

```yaml
platforms:
  meshtastic:
    enabled: true
    extra:
      transport: serial
      serial_port: /dev/ttyUSB0
      node_name: Hermes
      dm_policy: pairing          # open | pairing | allowlist | disabled
      group_policy: allowlist     # open | allowlist | disabled
      allow_from: ["!aabbccdd"]   # must be a list — allow_from: "*" is rejected
      text_chunk_limit: 200       # bytes, 50–230
      chunk_delay_seconds: 1.5
      channels:
        LongFast:
          require_mention: true
        Emergency:
          require_mention: false
```

Environment variables take precedence over `config.yaml`.

### Transports

**Serial (USB)** — the radio is plugged into the machine running Hermes.

**TCP (WiFi)** — the radio is reachable over the network by hostname or IP.
This replaces MeshClaw's `httpAddress`: the Python library speaks to the radio
over TCP rather than HTTP, but the user-facing capability is the same.

BLE and MQTT are **not supported in v1**. Configuring them fails at validation
with an explicit error rather than silently doing nothing — see
[ROADMAP.md](ROADMAP.md).

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

## Tools

The plugin registers four agent-callable tools.

| Tool | Access | Returns |
|---|---|---|
| `mesh_nodes` | read-only | known nodes: ID, names, hops, SNR/RSSI, battery, last heard, position |
| `mesh_telemetry` | read-only | battery, voltage, channel utilization, air-util-TX, temp/humidity/pressure, with recent history |
| `mesh_channels` | read-only | configured channels: index, name, primary |
| `mesh_send` | **gated** | transmit a message to a node or channel |

`mesh_send` is gated by `dm_policy`/`group_policy` and rate-limited (5 sends
per 60s). Airtime is a shared, legally regulated resource, and the agent must
not be able to flood it.

The same gate and rate limit apply to **every** outbound path — gateway
replies, the `mesh_send` tool, and cron/`send_message` delivery — so a
policy cannot be sidestepped by routing through a different tool.

### Position privacy

Node positions are the GPS coordinates of real people. MeshHermes:

- **never** logs positions,
- rounds coordinates to 4 decimal places (≈11 m) in tool output, configurable
  via `position_precision`,
- suppresses them entirely with `MESHTASTIC_EXPOSE_POSITION=false`.

## Hardware smoke test

Not run in CI — it needs a radio and it transmits.

```bash
python scripts/smoke_hardware.py --transport serial --port /dev/ttyUSB0
python scripts/smoke_hardware.py --transport tcp --host meshtastic.local
```

It connects, reads device info, checks the region, lists nodes, verifies
chunking by transmitting a >200-byte message to itself, waits for an inbound
DM from another node, then disconnects and confirms the port was released.

> **Transmitting requires a correctly-set LoRa region and compliance with your
> local RF regulations.**

## Tests

```bash
pip install -e ".[dev]"
pytest
```

The suite runs with **no radio attached**. A fake interface publishes on the
real pubsub topics, so the adapter's actual subscription wiring and its
thread→asyncio bridge are exercised rather than mocked away.

## Deliberate differences from MeshClaw

MeshHermes ports [MeshClaw](https://github.com/Seeed-Solution/MeshClaw)
(the equivalent plugin for OpenClaw). Some differences are intentional:

| Behaviour | MeshHermes | Why |
|---|---|---|
| **Chunking unit** | UTF-8 **bytes** | MeshClaw measures UTF-16 code units against a byte-sized LoRa frame, so emoji and CJK overflow it. This is a bug fix. |
| **Device rename (`setOwner`)** | not done | Renaming reboots the radio and forces a ~30s reconnect — hostile on a radio you may share. Use the `meshtastic` CLI. |
| **LoRa region** | read, never set | A partial `setConfig` can zero `tx_enabled` and disable TX. Choosing a region for the user is also a legal hazard. If it is `UNSET`, MeshHermes refuses to transmit and tells you to run `meshtastic --set lora.region <REGION>`. |
| **Reconnection** | gateway-owned | Hermes already runs a reconnect watcher with backoff. A second loop inside the adapter would race it for the serial port. |
| **Access control** | Hermes-native | Uses Hermes' policies and real pairing store instead of a parallel implementation. |
| **Multi-account** | Hermes profiles | Replaces MeshClaw's `accounts.ts`. |
| **Telemetry tools** | implemented | Listed as unbuilt roadmap in MeshClaw. |

## License

MIT — see [LICENSE](LICENSE).

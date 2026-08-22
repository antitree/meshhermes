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
| `MESHTASTIC_NODE_NAME` | no | mention trigger typed at the start of a message (`@` optional); defaults to the device's `longName`. The radio's real `longName`/`shortName` keep working too. |
| `MESHTASTIC_ALLOWED_USERS` | no | comma-separated `!hex` node IDs |
| `MESHTASTIC_ALLOW_ALL_USERS` | no | dev only — opens the bot to every node in range, **inbound and outbound** |
| `MESHTASTIC_HOME_CHANNEL` | no | cron/notification delivery target |
| `MESHTASTIC_EXPOSE_POSITION` | no | default `true`; `false` hides GPS in tool output |
| `MESHTASTIC_AUTO_INSTALL` | no | default `false`; `true` pip-installs `meshtastic` on connect if missing |

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
Hermes what's the weather looking like for tomorrow?
```

Meshtastic has no tagging protocol the way Discord or Slack do. Addressing a
node is just typing its name first, so that is exactly what the gate matches:

| Message | Wakes the agent? | Text the agent sees |
|---|---|---|
| `Hermes what's the weather?` | yes | `what's the weather?` |
| `@Hermes what's the weather?` | yes | `what's the weather?` |
| `Hermes: what's the weather?` | yes | `what's the weather?` |
| `Hermes, what's the weather?` | yes | `what's the weather?` |
| `hermes what's the weather?` | yes | `what's the weather?` |
| `HRM what's the weather?` (short name) | yes | `what's the weather?` |
| `ask Hermes about the weather` | no | -- |
| `Hermes` (bare) | passes the gate, but there is nothing to answer, so no reply | *(empty)* |

The radio's `shortName` triggers too, since typing a long name costs real
airtime. Short names are only honoured when they are at least 3 characters and
not an ordinary word (`ok`, `test`, `hey`, ...) - a cryptic 4-character name
should not be woken by every passing message.

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


## More documentation

The README covers getting running. The details live alongside it:

| Document | What's in it |
|---|---|
| [SECURITY.md](SECURITY.md) | Threat model, what's in and out of scope, how to report a vulnerability, and operator security notes |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development setup, running the tests without hardware, and the non-obvious traps in this codebase |
| [ROADMAP.md](ROADMAP.md) | BLE and MQTT transports, proactive alerts, and other unbuilt work |
| [CHANGELOG.md](CHANGELOG.md) | Release history |

A few things worth knowing before you deploy:

- **Access control is Hermes-native.** `dm_policy` and `group_policy` are
  enforced by the gateway's own authorization layer and pairing store, not
  reimplemented here. `dm_policy: open` requires an explicit `"*"` in
  `allow_from` so a radio is never opened by a config typo.
- **Node IDs are not authenticated** and channel traffic is readable by anyone
  holding the channel key. An allowlist raises the bar; it is not identity.
- **Positions are real people's coordinates.** They are never logged, are
  rounded to ~11 m in tool output, and can be suppressed entirely with
  `MESHTASTIC_EXPOSE_POSITION=false`.
- **BLE and MQTT are not supported yet** — configuring them fails at validation
  with a clear error rather than silently doing nothing.
- **A serial port cannot be shared.** If another program holds your radio, or
  ModemManager probes it on Linux, Hermes cannot open it.

Run the hardware check before trusting a deployment:

```bash
python scripts/smoke_hardware.py --transport serial --port /dev/ttyUSB0
```

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

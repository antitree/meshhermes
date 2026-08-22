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
# then: enable the plugin
hermes gateway setup      # choose Meshtastic
hermes gateway restart
```

`hermes plugins install` clones the repo into `~/.hermes/plugins/` and offers to
enable it. Third-party platform plugins are opt-in, so it stays off until you
say yes (or pass `--enable`).

The install itself asks nothing. Every Meshtastic setting is gathered by
`hermes gateway setup`, which writes them to `~/.hermes/.env`:

| Step | What happens |
|---|---|
| 1. enable the plugin | say yes at install, or pass `--enable`, or add `meshtastic-platform` to `plugins.enabled` in `~/.hermes/config.yaml` |
| 2. `hermes gateway setup` | choose Meshtastic and answer the questions — transport, radio address, bot name, access control, position privacy |
| 3. `hermes gateway restart` | picks up the new configuration and connects to the radio |

The wizard asks only what your answers make relevant: choose `tcp` and it
requires the radio's hostname or IP and offers the TCP port pre-filled with
`4403`; choose `serial` and it offers the detected USB device instead, and never
asks about a network address you do not have. That conditional shape is why the
install does not prompt — a flat list of install-time questions can only
under-ask or over-ask.

The wizard is also how you change any of it later. The full list of settings is
in [Environment variables](#environment-variables) below; setting those in
`~/.hermes/.env` by hand is equivalent to running the wizard.

If the gateway starts before it has been configured, it stops with a message
naming the missing variable and how to set it, rather than failing obscurely at
the radio.

<details>
<summary>Manual install</summary>

Cloning by hand replaces the first two steps above. The configuration step is
the same one either way.

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

The directory and the `plugins: enabled:` entry are `meshtastic-platform`,
while the platform itself is `meshtastic` (that is the name used under
`platforms:` below). The directory keeps the suffix on purpose: a directory
named `meshtastic` on `sys.path` would shadow the `meshtastic` pip package
this plugin imports.

Then configure it — the wizard, or the equivalent values in `~/.hermes/.env`:

```bash
hermes gateway setup      # choose Meshtastic
hermes gateway restart
```
</details>

## Configuration

There are a few config options that are requirements, the rest are optional.

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
| `MESHTASTIC_ALLOW_ALL_USERS` | yes | must be set explicitly: `true` opens the bot to every node in range (dev only), **inbound and outbound**; `false` restricts it to `MESHTASTIC_ALLOWED_USERS` |
| `MESHTASTIC_ALLOWED_USERS` | no | comma-separated `!hex` node IDs. Empty is valid — pair a node later. |
| `MESHTASTIC_TCP_HOST` | tcp only | hostname/IP, e.g. `meshtastic.local`. The gateway refuses to connect without it when `MESHTASTIC_TRANSPORT=tcp`. |
| `MESHTASTIC_TCP_PORT` | no | default `4403`; set it when the radio is behind a tunnel or reverse proxy |
| `MESHTASTIC_SERIAL_PORT` | no | e.g. `/dev/ttyUSB0`. Blank autodetects a single attached radio. |
| `MESHTASTIC_NODE_NAME` | no | mention trigger; defaults to the device's `longName` |
| `MESHTASTIC_HOME_CHANNEL` | no | cron/notification delivery target |
| `MESHTASTIC_EXPOSE_POSITION` | no | default `true`; `false` hides GPS in tool output |
| `MESHTASTIC_AUTO_INSTALL` | no | default `false`; `true` pip-installs `meshtastic` on connect if missing |
| `MESHTASTIC_CONVERSATION_COOLDOWN_SECONDS` | no | default `60`; quiet period per channel after the bot replies. `0` or negative disables it |
| `MESHTASTIC_COOLDOWN_EXEMPT_MENTIONS` | no | default `false`; `true` lets a message that names the bot skip the cooldown |
| `MESHTASTIC_LOOP_DETECTION` | no | default `false`; `true` refuses to answer the same text twice on a channel |
| `MESHTASTIC_LOOP_SIGNATURE_TTL_SECONDS` | no | default `600`; how long a message signature is remembered |
| `MESHTASTIC_LOOP_SIGNATURE_MAX_ENTRIES` | no | default `256`; hard cap on the signature cache |
| `MESHTASTIC_RATE_LIMIT_MAX_SENDS` | no | default `5`; hard cap on transmissions per window, across every send path |
| `MESHTASTIC_RATE_LIMIT_WINDOW_SECONDS` | no | default `60`; the rate-limit window |

Invalid values for any of these (non-numeric, zero, negative where that makes no
sense) fall back to the default with a warning in the log. A typo never turns a
limit off.

The required variables are checked before the gateway connects, so a
configuration that cannot reach the radio is refused with a message naming the
variable rather than failing obscurely at the radio — `hermes gateway setup`
runs the same check on what it saves, so a wizard run that reports success has
already been verified. Anything marked optional above is safe to leave unset:
each has a working default. Values already in `~/.hermes/.env` satisfy the
checks, so a pre-populated env file needs no wizard run at all.

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

## Acknowledgements

**Inspired by [MeshClaw](https://github.com/Seeed-Solution/MeshClaw)** by
[Seeed Studio](https://www.seeedstudio.com/) — the Meshtastic channel plugin for
OpenClaw. MeshClaw worked out the hard parts of bridging a LoRa mesh to an AI
agent: node-ID normalization, LoRa-sized chunking, channel and mention policy.
MeshHermes is an independent Python implementation of those ideas for Hermes
Agent, and its normalization, chunking, and policy modules follow MeshClaw's
design closely. MeshClaw is MIT licensed.


## License

MIT — see [LICENSE](LICENSE).

MeshHermes is an independent project. It is not affiliated with or endorsed by
Meshtastic, Seeed Studio, or Nous Research.

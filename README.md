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
# answer the prompts
hermes gateway restart
```

`hermes plugins install` clones the repo into `~/.hermes/plugins/` and offers to
enable it. Third-party platform plugins are opt-in, so it stays off until you
say yes (or pass `--enable`).

The install prompts for the settings the plugin cannot run without —
`meshtastic-platform requires the following environment variables` — and writes
your answers to `~/.hermes/.env`:

| Setting | Answer |
|---|---|
| `MESHTASTIC_TRANSPORT` | `serial` for a USB radio, `tcp` for one on WiFi |
| `MESHTASTIC_ALLOW_ALL_USERS` | `false` unless you mean to let every node in radio range command the bot |
| `MESHTASTIC_TCP_HOST` | the radio's hostname or IP, e.g. `meshtastic.local`. Asked only after you answer `tcp`, and required then; never asked for `serial`. |
| `MESHTASTIC_TCP_PORT` | asked only after you answer `tcp`, pre-filled with `4403` — press enter unless the radio is behind a tunnel or reverse proxy |

Then restart the gateway and it is running. There is no separate configuration
step: everything else has a working default.

A value already present in `~/.hermes/.env` is not asked for again, so writing
those settings there ahead of time makes the install non-interactive.

To change any of it later, or to reach the settings the install does not ask
about — access-control detail, position privacy, the home channel for cron — run
the wizard:

```bash
hermes gateway setup      # choose Meshtastic
hermes gateway restart
```

The full list of settings is in [Environment variables](#environment-variables)
below; the wizard covers the same ground interactively.

<details>
<summary>Manual install</summary>

A manual install never sees the install prompts, so it needs the configuration
step the plugin install does for you.

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
| `MESHTASTIC_TCP_HOST` | tcp only | hostname/IP, e.g. `meshtastic.local`. Asked for during a tcp install; install and connect both fail without it when `MESHTASTIC_TRANSPORT=tcp`. |
| `MESHTASTIC_TCP_PORT` | no | default `4403`, offered pre-filled during a tcp install; change it when the radio is behind a tunnel or reverse proxy |
| `MESHTASTIC_SERIAL_PORT` | no | e.g. `/dev/ttyUSB0`. Blank autodetects a single attached radio. |
| `MESHTASTIC_NODE_NAME` | no | mention trigger; defaults to the device's `longName` |
| `MESHTASTIC_HOME_CHANNEL` | no | cron/notification delivery target |
| `MESHTASTIC_EXPOSE_POSITION` | no | default `true`; `false` hides GPS in tool output |
| `MESHTASTIC_AUTO_INSTALL` | no | default `false`; `true` pip-installs `meshtastic` on connect if missing |

The required variables are checked before an install completes, so a
configuration that cannot reach the radio is refused up front rather than
failing later at connect time — that check is what the install prompts satisfy.
Choosing `tcp` prompts for the radio's hostname or IP, which is mandatory and
re-asked until it is given, and then for the TCP port, pre-filled with `4403`
so pressing enter keeps the standard value. Choosing `serial` asks neither
question. Anything marked optional above is safe to leave unset: each has a
working default. Values already in `~/.hermes/.env` satisfy the checks without
any prompting.

In a non-interactive install (piped stdin, CI, `--yes`) there is nobody to ask,
so a missing `MESHTASTIC_TCP_HOST` fails immediately with the exact line to add
to `~/.hermes/.env` rather than waiting on input.

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

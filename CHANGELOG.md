# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Breaking
- **Installing the plugin no longer prompts for anything, and this
  reverses part of the 1.0.0 install behaviour.** 1.0.0 listed
  `MESHTASTIC_TRANSPORT` and `MESHTASTIC_ALLOW_ALL_USERS` in
  `plugin.yaml`'s `requires_env` so Hermes' generic installer would ask for
  them inline. Both have moved back to `optional_env`, `requires_env` is
  gone entirely, and `required_env` is no longer passed to
  `register_platform()`. Anyone whose install scripting depended on being
  prompted must now run `hermes gateway setup` — or pre-populate
  `~/.hermes/.env`, which was always equivalent.

  The reason is that both of those lists are flat, and the requirements are
  not: a TCP host is mandatory, but only once the transport is `tcp`. A
  flat prompt list can therefore only under-ask (a tcp install with no
  host, which fails later at the radio) or over-ask (a serial operator made
  to supply a network address they do not have). `hermes gateway setup` is
  wired through `setup_fn`, is the one configuration path whose invocation
  is verifiable from this repo, and does know the conditional rules — so it
  is now the canonical way to configure MeshHermes, rather than an optional
  reconfigure step.

  Nothing is lost by not asking: `envcheck.py`'s rules still run from
  `validate_config()` and `connect()`, so a gateway started with a
  configuration that cannot reach the radio still fails immediately with a
  message naming the variable and both ways to fix it.
- This also supersedes the documentation change in 1.0.0 that removed
  `hermes gateway setup` from the required install path. That removal was
  correct while the install prompted inline; it no longer does, so the
  wizard returns to the documented flow: install → enable → `hermes gateway
  setup` → `hermes gateway restart`.

- **Mention matching is leading-position only.** A mid-sentence `@name`
  no longer counts as addressing the bot ("tell @hermes I said hi" is
  conversation *about* the bot, not an instruction *to* it). Deployments
  that relied on the old mid-sentence fallback must move the name to the
  start of the message.

### Changed
- **The Meshtastic gateway's icon is a bunny rabbit** (🐰), replacing the
  radio emoji, and `plugin.yaml` now carries it in an `icon:` key alongside
  the `emoji=` that `register_platform()` has always taken. A test keeps
  the two in sync.
- **`hermes gateway setup` is now the canonical configuration path**, not an
  optional reconfigure step, and its documentation says so. It already
  asked for everything and already re-validated what it saved; what changed
  is that nothing else asks any more.
- `check_env_ready()` remains as a pure, non-prompting check of whether a
  configuration is coherent, and is no longer described or used as an
  install gate.
- **Mention gating now matches how Meshtastic actually works.** There is no
  tagging protocol on a mesh channel - people address a node by typing its
  name first. The old IRC-style patterns required `@name` or `name:`/`name,`,
  so a bare `Long Name of Node tell me the weather` fell through the gate
  entirely. The unified matcher accepts the name at the start of the
  message, case-insensitive, with an optional leading `@` and optional
  trailing `:`/`,`/whitespace. The IRC forms still work as a consequence of
  that rule rather than as separately maintained patterns.
- **The radio's `shortName` is a mention trigger too**, guarded against
  false wakes: a short name is ignored when it is empty, under
  `MIN_SHORT_NAME_LENGTH` (3) characters, or a common word listed in
  `SHORT_NAME_STOPWORDS`.
- **A configured `MESHTASTIC_NODE_NAME` no longer hides the radio's real
  name.** The custom trigger is primary, but the device's own
  `longName`/`shortName` still reach the bot.

### Added
- `adapter.POST_INSTALL_MESSAGE` / `post_install_message()`: the three steps
  to take once the plugin is on disk — enable it, `hermes gateway setup`,
  `hermes gateway restart` — as a plain non-prompting string, also exported
  from the package. Whether Hermes surfaces it during
  `hermes plugins install` is not something this repo can verify; README.md
  is the reliable source of truth for the flow, and a test keeps the two in
  agreement.
- `tests/test_setup_wizard.py`: coverage for `hermes gateway setup` itself,
  which had none. It drives the wizard against a stand-in for
  `hermes_cli.setup` and asserts that a run covers the full mandatory set
  for both transports, that a tcp run requires a host and offers the port
  pre-filled with `4403`, and that a serial run never asks for a network
  address.
- Loop prevention for bots sharing a channel with `require_mention: false`.
  Two Hermes bots on one channel would otherwise answer each other forever,
  burning shared and legally regulated airtime. Three independent controls,
  each sufficient on its own:
  - **Conversation cooldown** (on, 60s, `MESHTASTIC_CONVERSATION_COOLDOWN_SECONDS`):
    after replying on a channel the bot stays quiet there for the cooldown.
    Applies to *all* replies including mentions; `MESHTASTIC_COOLDOWN_EXEMPT_MENTIONS`
    (default off) relaxes that. `0` or negative disables the control.
  - **Loop-signature detection** (off, `MESHTASTIC_LOOP_DETECTION`): refuses
    to answer text already seen on that channel. Keys on `(channel,
    case-and-whitespace-normalized text)` — deliberately not the sender,
    since the two bots in a loop are different senders saying the same
    thing. Cache bounded by TTL (`MESHTASTIC_LOOP_SIGNATURE_TTL_SECONDS`,
    600s) and a hard entry cap (`MESHTASTIC_LOOP_SIGNATURE_MAX_ENTRIES`, 256).
  - **Hard rate limit**, now configurable via `MESHTASTIC_RATE_LIMIT_MAX_SENDS`
    (5) and `MESHTASTIC_RATE_LIMIT_WINDOW_SECONDS` (60). Defaults unchanged.

  Suppressed replies are dropped silently and logged at INFO with the control
  that fired — nothing is sent on-channel, since an error message is itself
  airtime and something the other bot could reply to.
- `tests/fake_mesh.py` grows a `SharedAir`: several fake radios on one
  channel, where each transmission is delivered to the others. This makes
  the two-bot runaway reproducible in a test, with a transmission budget so
  a regression fails loudly instead of hanging the suite.
- `envcheck.py`: one declarative rule set for which `MESHTASTIC_*` variables
  are mandatory, including conditional ones. `plugin.yaml`'s schema cannot
  say "required only when transport is tcp", which is why the install
  prompts for nothing at all. The rules are enforced by the plugin itself,
  shared by the setup wizard, `validate_config()`, and `connect()`, so a
  gateway configured with `MESHTASTIC_TRANSPORT=tcp` and no host fails
  immediately with a message naming the variable rather than failing
  obscurely at the radio.
- `MESHTASTIC_TCP_PORT` (default `4403`) is now read and honoured. The TCP
  port was previously hardcoded by the library default, so a radio behind
  an SSH tunnel or reverse proxy was unreachable. The wizard prompts for it
  during tcp setup.
- `CONTRIBUTING.md`, `SECURITY.md`, and this changelog.
- Pre-commit configuration with secret detection, and `.gitignore` rules
  blocking credential files.
- `sendpolicy.py`: one authorization gate shared by all three outbound
  paths (gateway reply, `mesh_send` tool, cron/standalone sender).
- `MESHTASTIC_AUTO_INSTALL` (default off) opts in to installing the
  `meshtastic` package on connect. `ensure_deps()` previously existed but
  nothing called it — `PlatformEntry` has no `ensure_deps_fn` hook.
- `allow_update_command=True` is now passed explicitly at registration.
- A test asserting every `register_platform` kwarg is a real
  `PlatformEntry` field.

### Fixed
- `adapter.send()` — the gateway's own reply path — did not call the shared
  rate limit, despite `sendpolicy`'s docstring promising all three outbound
  paths share one gate. The busiest path was uncapped.
- `mesh_tools.py` aliased `RATE_LIMIT_MAX_SENDS` / `RATE_LIMIT_WINDOW_SECONDS`
  by value at import time. With the limits now operator-configurable that
  would have frozen whatever the environment said at first import, and made
  the tool's error messages quote a limit the gate was not applying.

### Changed
- README documents the install flow that actually happens. This was
  rewritten twice within this release: first to match an install that
  prompted inline, and then — once the plugin stopped prompting at all —
  to the flow that now stands, install → enable → `hermes gateway setup` →
  `hermes gateway restart`. See the Breaking section above.
- `MESHTASTIC_ALLOW_ALL_USERS` must now be set explicitly.
  Left unset it either silently opened the bot to every node in radio range
  or left it unable to answer anyone, with no indication which. An empty
  `MESHTASTIC_ALLOWED_USERS` with `MESHTASTIC_ALLOW_ALL_USERS=false` remains
  valid — the "pair a node later" workflow is unchanged. This is an
  install-time requirement only, so existing deployments keep running.
- A missing required variable now fails with a message naming the variable,
  the condition that made it required, and both ways to set it.
- Test fixtures use a neutral channel name instead of one from the author's
  own mesh.
- `MESHTASTIC_ALLOW_ALL_USERS` is now honoured by the outbound send gate,
  not only by Hermes' inbound authorization.

### Security
Fixes from a security review (MH-SEC-001 … 009):

- **Cron/standalone sends were ungated.** `_standalone_send()` applied no
  DM policy, group policy, allowlist, or rate limit. It is reachable from
  `tools/send_message_tool.py` — the same agent surface as `mesh_send` — so
  an agent could bypass every control by choosing the other tool.
- **Sends continued after connection loss.** `_iface` stays set when the
  link drops, and both `send()` and `mesh_send` checked only that, not
  `is_connected`.
- **`mesh_send` reported success for sends that then failed.** Called from
  the gateway loop it cannot await, so it reports acceptance; destination
  resolution now happens *before* that report, instead of after.
- **`dm_policy: open` was unenforced at runtime.** `validate_config()`
  requires an explicit `"*"` in `allow_from`; an adapter built directly
  skipped that check entirely when sending.
- **Sender identity trusted the spoofable string.** `fromId` won over the
  numeric `from`. Authorization now uses the numeric node number, and a
  packet whose two identities disagree is dropped.
- **A leading `/` bypassed mention gating.** Any node on a shared channel
  could wake the agent regardless of `require_mention`, because
  attacker-controlled text was treated as an "authorized command".
- **`position_precision` was unclamped**, so a config typo could publish
  metre-accurate coordinates of real people. Clamped to ≤4 dp (~11 m).
- **`bridge_to_loop()` discarded its future**, so an exception on the
  receive thread vanished with no log line.
- **`allow_from` given as a string was silently mis-parsed.** Python
  iterates a string into characters, so `allow_from: "*"` became `["*"]`
  and satisfied the open-policy guard — opening the radio to the entire
  mesh from a YAML typo — while `allow_from: "!aabbccdd"` became
  per-character garbage matching nothing. Both spellings are now rejected
  with an explicit error. Found by comparison against an independent
  implementation that type-checked this input.

## [1.0.0] - 2026-08-18

First working release: a Meshtastic LoRa platform plugin for Hermes Agent,
verified end to end against a real RAK4631 radio.

### Added
- Serial (USB) and TCP (WiFi) transports.
- Direct messages and channel messages, with mention gating and per-channel
  `require_mention` overrides.
- Byte-accurate LoRa chunking with markdown stripping and configurable
  inter-chunk pacing (default 1.5 s).
- Four agent-callable tools: `mesh_nodes`, `mesh_telemetry`,
  `mesh_channels`, and a policy-gated, rate-limited `mesh_send`.
- Position privacy: coordinates rounded to ~11 m by default, never logged,
  and suppressible entirely via `MESHTASTIC_EXPOSE_POSITION=false`.
- Cron delivery support via `cron_deliver_env_var` and a standalone sender
  for out-of-process delivery.
- Interactive setup wizard and a guided hardware smoke-test script.
- 281 tests that run with no radio and no Hermes install attached.

### Security
- Refuses to transmit when the LoRa region is `UNSET`, directing the
  operator to set it with the `meshtastic` CLI. The plugin never sets the
  region itself, because a partial `setConfig` can zero `tx_enabled`.
- `dm_policy: open` requires an explicit `"*"` in `allow_from`, so a radio
  is never opened to the whole mesh by a configuration typo.
- Access control is delegated to Hermes' authorization layer and pairing
  store rather than reimplemented.

### Fixed
Three defects found only by testing against real hardware — each passed the
full test suite while being broken on a device:

- **Region and channel state read from the wrong object.** On a real radio
  `iface.localConfig` and `iface.channels` are both `None`; the data lives
  under `iface.localNode`. This silently disabled the `UNSET`-region check
  that prevents non-compliant transmission, and broke all channel
  name/index resolution.
- **Sends to an unconfigured channel silently retargeted.** A channel name
  absent from the radio fell back to index 0, transmitting on the primary
  channel while reporting success for a channel that did not exist.
- **An empty `chat_id` broadcast mesh-wide.** An unresolvable target fell
  through to a broadcast instead of erroring.

### Notes on parity with MeshClaw
This plugin ports [MeshClaw](https://github.com/Seeed-Solution/MeshClaw)
(the equivalent for OpenClaw). Deliberate differences, with rationale, are
documented in the README. The most significant: chunking is measured in
UTF-8 bytes rather than UTF-16 code units, fixing frame overflow on emoji
and CJK text.

BLE and MQTT transports are not implemented; configuring them fails
validation with an explicit error rather than silently. See `ROADMAP.md`.

[Unreleased]: https://github.com/antitree/meshhermes/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/antitree/meshhermes/releases/tag/v1.0.0

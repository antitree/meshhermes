# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Report privately through GitHub's [Report a
vulnerability](https://github.com/antitree/meshhermes/security/advisories/new)
button on the Security tab. If that is unavailable to you, open a minimal
issue asking for a private contact channel — without details.

Expect an acknowledgement within 7 days. Please allow 90 days before public
disclosure so a fix can ship.

## Supported versions

Pre-1.0 in practice: only the latest release on `main` receives fixes.

## What is in scope

This plugin connects an LLM agent to a **physical radio transmitter** that
carries other people's traffic. The threat model is unusual, so these
categories matter more than in a typical Python package:

| Area | Why it matters |
|---|---|
| **Unintended transmission** | Anything that causes the radio to transmit when it should not, transmit to the wrong destination, or transmit without the operator's intent. Airtime is a shared, legally regulated resource. |
| **Access-control bypass** | A path that lets an unauthorized node command the agent, or that circumvents `dm_policy` / `group_policy` / the pairing store. |
| **Position disclosure** | Anything that leaks node GPS coordinates past the configured rounding or the `MESHTASTIC_EXPOSE_POSITION=false` switch, including via logs. |
| **Prompt-injection into transmission** | A mesh user crafting a message that induces the agent to transmit attacker-chosen content, spam the mesh, or exfiltrate data over the radio. |
| **Region / regulatory bypass** | Anything that transmits when the LoRa region is `UNSET`, or that modifies region or TX settings. The plugin deliberately never sets these. |
| **Credential handling** | Leakage of Hermes credentials reachable from plugin code. |

## What is out of scope

- **Meshtastic protocol and firmware weaknesses.** Report those to the
  [Meshtastic project](https://github.com/meshtastic). Notably, LoRa
  channel encryption uses a pre-shared key: anyone with the channel PSK can
  read and forge traffic. That is a property of Meshtastic, not a bug here.
- **Hermes Agent core vulnerabilities.** Report to
  [hermes-agent](https://github.com/NousResearch/hermes-agent).
- **Physical access to the radio.** Anyone holding the device can
  reconfigure it.
- **Consequences of deliberately permissive configuration** — for example
  `dm_policy: open` with `allow_from: ["*"]`, or
  `MESHTASTIC_ALLOW_ALL_USERS=true`. These are documented as opening the
  bot to every node in radio range.

## Operator security notes

Worth understanding before deploying:

1. **Node IDs are not authenticated.** Meshtastic node IDs can be spoofed.
   An allowlist raises the bar; it is not an identity guarantee.
2. **Channel traffic is readable by anyone with the PSK**, including any
   reply the agent transmits. Do not send secrets over the mesh.
3. **A mesh user is an untrusted party talking to your agent.** Scope the
   agent's toolset accordingly — it may have capabilities well beyond the
   radio.
4. **Positions are real people's locations.** Default rounding is ~11 m;
   `MESHTASTIC_EXPOSE_POSITION=false` suppresses them entirely.
5. **You are responsible for RF compliance** in your jurisdiction,
   including duty-cycle limits and correct region configuration.

"""Interactive setup for ``hermes gateway setup``.

This is **the** configuration path for MeshHermes.  Installing the plugin
asks nothing: a flat list of install-time prompts cannot express "a TCP
host is mandatory, but only once you have chosen tcp", so it can only
under-ask or over-ask.  This wizard knows the conditional rules — it shares
them with the runtime via :mod:`envcheck` — and re-validates what it wrote
before reporting success.

Ports MeshClaw's wizard, minus two steps that are deliberately absent:

- **No region step.**  Setting the LoRa region from a plugin is a legal and
  usability hazard: a partial ``setConfig`` can zero ``tx_enabled``.  The
  user runs ``meshtastic --set lora.region <REGION>`` themselves.
- **No display-name step.**  Hermes profiles cover what MeshClaw's
  multi-account naming did.

CLI helpers are imported lazily so this module stays importable in the
gateway runtime and in tests.
"""

from __future__ import annotations

import os
from typing import List

# The requirement rules are shared with the install-time check and the
# adapter so the wizard cannot drift from what is actually enforced.
try:
    from .envcheck import (
        DEFAULT_TCP_PORT,
        SUPPORTED_TRANSPORTS,
        UNSUPPORTED_TRANSPORTS,
        validate_env,
    )
except ImportError:  # pragma: no cover - direct-import context
    from envcheck import (  # type: ignore[no-redef]
        DEFAULT_TCP_PORT,
        SUPPORTED_TRANSPORTS,
        UNSUPPORTED_TRANSPORTS,
        validate_env,
    )


def _detect_serial_ports() -> List[str]:
    """Best-effort list of attached radios.  Never raises."""
    try:
        from meshtastic.util import findPorts

        return list(findPorts(True) or [])
    except Exception:
        return []


def _saved_env(get_env_value) -> dict:
    """Read back every rule-governed variable from the env file.

    The wizard writes to ``~/.hermes/.env``, which does not update
    ``os.environ`` in this process, so the check has to read through the
    CLI's accessor rather than trusting the ambient environment.
    """
    try:
        from .envcheck import ENV_RULES
    except ImportError:  # pragma: no cover - direct-import context
        from envcheck import ENV_RULES  # type: ignore[no-redef]

    return {rule.name: (get_env_value(rule.name) or "") for rule in ENV_RULES}


def interactive_setup() -> None:
    """Prompt for Meshtastic settings and save them to ``~/.hermes/.env``."""
    from hermes_cli.setup import (
        get_env_value,
        print_header,
        print_info,
        print_success,
        print_warning,
        prompt,
        prompt_yes_no,
        save_env_value,
    )

    print_header("Meshtastic")

    existing = get_env_value("MESHTASTIC_TRANSPORT")
    if existing:
        print_info(f"Meshtastic: already configured (transport: {existing})")
        if not prompt_yes_no("Reconfigure Meshtastic?", False):
            return

    print_info("Connect Hermes to a Meshtastic LoRa mesh radio.")
    print_info("   Requires the meshtastic package: pip install meshtastic")
    print()

    # ── Transport ─────────────────────────────────────────────────────────
    print_info("Transport:")
    print_info("   serial — radio attached over USB")
    print_info("   tcp    — radio reachable over WiFi by hostname/IP")
    transport = (prompt("Transport (serial/tcp)", default=existing or "serial") or "").strip().lower()

    if transport in UNSUPPORTED_TRANSPORTS:
        print_warning(f"'{transport}' is not supported in v1 — see ROADMAP.md. Use serial or tcp.")
        return
    if transport not in SUPPORTED_TRANSPORTS:
        print_warning(f"Unknown transport '{transport}' — skipping Meshtastic setup")
        return
    save_env_value("MESHTASTIC_TRANSPORT", transport)

    if transport == "serial":
        ports = _detect_serial_ports()
        if ports:
            print_info(f"Detected serial port(s): {', '.join(ports)}")
        else:
            print_info("No radio auto-detected — plug it in, or enter the path manually.")
        default_port = get_env_value("MESHTASTIC_SERIAL_PORT") or (ports[0] if ports else "")
        serial_port = prompt(
            "Serial port (blank = autodetect a single attached radio)",
            default=default_port,
        )
        save_env_value("MESHTASTIC_SERIAL_PORT", (serial_port or "").strip())
    else:
        tcp_host = prompt(
            "Radio hostname or IP (e.g. meshtastic.local)",
            default=get_env_value("MESHTASTIC_TCP_HOST") or "",
        )
        if not tcp_host:
            print_warning("A host is required for tcp transport — skipping Meshtastic setup")
            return
        save_env_value("MESHTASTIC_TCP_HOST", tcp_host.strip())

        # Optional — a stock radio listens on 4403.  Prompted anyway
        # because a radio behind an SSH tunnel or reverse proxy is not on
        # the default port, and that is invisible until nothing connects.
        tcp_port = prompt(
            "Radio TCP port",
            default=get_env_value("MESHTASTIC_TCP_PORT") or str(DEFAULT_TCP_PORT),
        )
        save_env_value(
            "MESHTASTIC_TCP_PORT", (tcp_port or "").strip() or str(DEFAULT_TCP_PORT)
        )

    # ── Identity ──────────────────────────────────────────────────────────
    print()
    print_info("🏷️  Bot name on the mesh")
    print_info("   Meshtastic has no mention protocol: people address a node by")
    print_info("   typing its name at the START of the message. Both 'Hermes status'")
    print_info("   and '@Hermes status' work - the @ is optional, case does not")
    print_info("   matter, and the radio's short name works too.")
    print_info("   Leave blank to use the radio's own long name.")
    print_info("   Note: this plugin never renames your radio — use the")
    print_info("   meshtastic CLI if you want to change the device name.")
    node_name = prompt("Bot node name (optional)", default=get_env_value("MESHTASTIC_NODE_NAME") or "")
    save_env_value("MESHTASTIC_NODE_NAME", (node_name or "").strip())

    # ── Access control ────────────────────────────────────────────────────
    print()
    print_info("🔒 Access control")
    print_info("   A radio channel is public: anyone in range can hear it and")
    print_info("   transmit on it. Node IDs are not authenticated.")

    allow_all = prompt_yes_no("Allow ANY node on the mesh to command the bot? (dev only)", False)
    if allow_all:
        save_env_value("MESHTASTIC_ALLOW_ALL_USERS", "true")
        save_env_value("MESHTASTIC_ALLOWED_USERS", "")
        print_warning("⚠️  Open access — any node in radio range can command the bot.")
    else:
        save_env_value("MESHTASTIC_ALLOW_ALL_USERS", "false")
        allowed = prompt(
            "Allowed node IDs (comma-separated, e.g. !aabbccdd)",
            default=get_env_value("MESHTASTIC_ALLOWED_USERS") or "",
        )
        if allowed:
            save_env_value("MESHTASTIC_ALLOWED_USERS", allowed.replace(" ", ""))
            print_success("Allowlist configured")
        else:
            save_env_value("MESHTASTIC_ALLOWED_USERS", "")
            print_info("No nodes allowed yet — pair a node or add IDs to start.")

    # ── Cron delivery ─────────────────────────────────────────────────────
    print()
    home = prompt(
        "Home channel for cron/notification delivery (e.g. channel:LongFast, optional)",
        default=get_env_value("MESHTASTIC_HOME_CHANNEL") or "",
    )
    if home:
        save_env_value("MESHTASTIC_HOME_CHANNEL", home.strip())

    # ── Privacy ───────────────────────────────────────────────────────────
    print()
    print_info("📍 Node positions are the GPS coordinates of real people.")
    print_info("   When exposed, they are rounded to ~11 m in tool output.")
    expose = prompt_yes_no("Expose node positions to the agent?", True)
    save_env_value("MESHTASTIC_EXPOSE_POSITION", "true" if expose else "false")

    # Re-check what was just written against the same rules the installer
    # and the adapter enforce.  A wizard that reports success on a config
    # that cannot connect is the exact failure this validation exists to
    # prevent, so it must verify rather than assume.
    problem = validate_env(_saved_env(get_env_value))
    if problem:
        print()
        print_warning(problem)
        print_warning(
            "Meshtastic is saved but incomplete — rerun: hermes gateway setup"
        )
        return

    print()
    print_success("Meshtastic configuration saved to ~/.hermes/.env")
    print_warning(
        "Before transmitting, make sure your radio's LoRa region is set: "
        "meshtastic --set lora.region <REGION>"
    )
    print_info("Transmitting is subject to your local RF regulations.")
    print_info("Restart the gateway to apply: hermes gateway restart")

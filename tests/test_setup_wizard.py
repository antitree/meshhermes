"""Tests for ``hermes gateway setup`` — now the only configuration path.

Installing this plugin asks nothing: a flat list of install-time prompts
cannot express "a TCP host is mandatory, but only once you have chosen
tcp", so it can only under-ask or over-ask.  That makes the wizard load
bearing.  If it fails to ask for something, there is no other interactive
surface that will.

These drive :func:`setup_wizard.interactive_setup` against a stand-in for
``hermes_cli.setup``, recording every question asked and every value
saved, and assert that a run covers the full required set for both
transports.
"""

from __future__ import annotations

import sys
import types

import pytest

import envcheck
import setup_wizard


class FakeCli:
    """Stands in for ``hermes_cli.setup``.

    Answers come from *answers*, keyed by a substring of the prompt text,
    so a test states intent ("the host is radio.local") rather than
    depending on exact wording.  An unmatched prompt takes its default,
    which is what pressing enter does.
    """

    def __init__(self, answers=None, yes_no=None, existing=None):
        self.answers = answers or {}
        self.yes_no = yes_no or {}
        self.existing = dict(existing or {})
        self.saved: dict = {}
        self.asked: list = []
        self.warnings: list = []
        self.infos: list = []

    # -- the accessors the wizard imports ------------------------------
    def get_env_value(self, name):
        return self.existing.get(name, "")

    def save_env_value(self, name, value):
        self.saved[name] = value
        self.existing[name] = value

    def prompt(self, text, default=""):
        self.asked.append((text, default))
        for needle, value in self.answers.items():
            if needle.lower() in text.lower():
                return value
        return default

    def prompt_yes_no(self, text, default=False):
        self.asked.append((text, default))
        for needle, value in self.yes_no.items():
            if needle.lower() in text.lower():
                return value
        return default

    def print_header(self, *a, **k):
        pass

    def print_info(self, *a, **k):
        self.infos.append(" ".join(str(x) for x in a))

    def print_success(self, *a, **k):
        self.infos.append(" ".join(str(x) for x in a))

    def print_warning(self, *a, **k):
        self.warnings.append(" ".join(str(x) for x in a))


def _install(instance, monkeypatch):
    pkg = types.ModuleType("hermes_cli")
    mod = types.ModuleType("hermes_cli.setup")
    for name in (
        "get_env_value",
        "save_env_value",
        "prompt",
        "prompt_yes_no",
        "print_header",
        "print_info",
        "print_success",
        "print_warning",
    ):
        setattr(mod, name, getattr(instance, name))
    pkg.setup = mod
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.setup", mod)


@pytest.fixture
def cli(monkeypatch):
    """Install a fake ``hermes_cli.setup`` for the duration of one test."""
    fake = FakeCli()
    _install(fake, monkeypatch)
    # No radio attached in CI; keep autodetect deterministic.
    monkeypatch.setattr(setup_wizard, "_detect_serial_ports", lambda: [])
    return fake


def run(cli_obj, answers=None, yes_no=None, existing=None):
    cli_obj.answers = answers or {}
    cli_obj.yes_no = yes_no or {}
    cli_obj.existing = dict(existing or {})
    setup_wizard.interactive_setup()
    return cli_obj


def asked_text(cli_obj) -> str:
    return "\n".join(text for text, _ in cli_obj.asked).lower()


class TestSerialRun:
    def test_a_complete_serial_run_saves_a_valid_configuration(self, cli):
        run(cli, answers={"transport": "serial", "serial port": "/dev/ttyUSB0"})
        assert cli.saved["MESHTASTIC_TRANSPORT"] == "serial"
        assert cli.saved["MESHTASTIC_SERIAL_PORT"] == "/dev/ttyUSB0"
        # The wizard's own re-check is the same one the runtime enforces.
        assert envcheck.validate_env(cli.saved) is None

    def test_serial_run_never_asks_for_a_tcp_host(self, cli):
        """Over-asking is the other half of the flat-prompt-list problem."""
        run(cli, answers={"transport": "serial"})
        assert "hostname" not in asked_text(cli)
        assert "tcp port" not in asked_text(cli)

    def test_blank_serial_port_is_accepted_as_autodetect(self, cli):
        run(cli, answers={"transport": "serial", "serial port": ""})
        assert cli.saved["MESHTASTIC_SERIAL_PORT"] == ""
        assert envcheck.validate_env(cli.saved) is None


class TestTcpRun:
    def test_a_complete_tcp_run_saves_a_valid_configuration(self, cli):
        run(cli, answers={"transport": "tcp", "hostname": "radio.local"})
        assert cli.saved["MESHTASTIC_TRANSPORT"] == "tcp"
        assert cli.saved["MESHTASTIC_TCP_HOST"] == "radio.local"
        assert envcheck.validate_env(cli.saved) is None

    def test_the_host_is_mandatory_for_tcp(self, cli):
        """No host, no save of a configuration that claims to work."""
        run(cli, answers={"transport": "tcp", "hostname": ""})
        assert not cli.saved.get("MESHTASTIC_TCP_HOST")
        assert cli.warnings, "an empty host must be reported, not accepted"
        assert any("host" in w.lower() for w in cli.warnings)

    def test_the_port_is_offered_pre_filled_with_4403(self, cli):
        """Pressing enter is the common case; changing it must be possible."""
        run(cli, answers={"transport": "tcp", "hostname": "radio.local"})
        port_prompts = [
            (text, default) for text, default in cli.asked if "tcp port" in text.lower()
        ]
        assert port_prompts, "the TCP port must be offered"
        assert port_prompts[0][1] == str(envcheck.DEFAULT_TCP_PORT)
        assert cli.saved["MESHTASTIC_TCP_PORT"] == str(envcheck.DEFAULT_TCP_PORT)

    def test_a_non_default_port_is_kept(self, cli):
        """A radio behind a tunnel is not on 4403."""
        run(
            cli,
            answers={"transport": "tcp", "hostname": "radio.local", "tcp port": "8403"},
        )
        assert cli.saved["MESHTASTIC_TCP_PORT"] == "8403"
        assert envcheck.validate_env(cli.saved) is None


class TestCoversTheFullRequiredSet:
    """The wizard is the only interactive surface, so it must cover it all."""

    def test_every_mandatory_variable_is_saved_on_a_tcp_run(self, cli):
        run(cli, answers={"transport": "tcp", "hostname": "radio.local"})
        mandatory = {
            rule.name
            for rule in envcheck.ENV_RULES
            if rule.required_when({"MESHTASTIC_TRANSPORT": "tcp"})
        }
        assert mandatory <= set(cli.saved)

    def test_every_mandatory_variable_is_saved_on_a_serial_run(self, cli):
        run(cli, answers={"transport": "serial"})
        mandatory = {
            rule.name
            for rule in envcheck.ENV_RULES
            if rule.required_when({"MESHTASTIC_TRANSPORT": "serial"})
        }
        assert mandatory <= set(cli.saved)

    @pytest.mark.parametrize(
        "name",
        [
            "MESHTASTIC_TRANSPORT",
            "MESHTASTIC_TCP_HOST",
            "MESHTASTIC_TCP_PORT",
            "MESHTASTIC_NODE_NAME",
            "MESHTASTIC_ALLOW_ALL_USERS",
            "MESHTASTIC_ALLOWED_USERS",
            "MESHTASTIC_EXPOSE_POSITION",
        ],
    )
    def test_tcp_run_covers_the_documented_settings(self, cli, name):
        run(cli, answers={"transport": "tcp", "hostname": "radio.local"})
        assert name in cli.saved

    def test_the_home_channel_is_offered(self, cli):
        run(
            cli,
            answers={"transport": "serial", "home channel": "channel:LongFast"},
        )
        assert cli.saved["MESHTASTIC_HOME_CHANNEL"] == "channel:LongFast"

    def test_access_control_is_asked_not_defaulted_silently(self, cli):
        run(cli, answers={"transport": "serial"})
        assert "allow any node" in asked_text(cli)
        assert cli.saved["MESHTASTIC_ALLOW_ALL_USERS"] in ("true", "false")

    def test_allow_all_yes_records_the_open_choice(self, cli):
        run(cli, answers={"transport": "serial"}, yes_no={"allow any node": True})
        assert cli.saved["MESHTASTIC_ALLOW_ALL_USERS"] == "true"
        assert any("open access" in w.lower() for w in cli.warnings)

    def test_an_allowlist_is_recorded_when_access_is_closed(self, cli):
        run(
            cli,
            answers={"transport": "serial", "allowed node ids": "!aabbccdd, !11223344"},
            yes_no={"allow any node": False},
        )
        assert cli.saved["MESHTASTIC_ALLOWED_USERS"] == "!aabbccdd,!11223344"

    def test_position_privacy_is_asked(self, cli):
        run(cli, answers={"transport": "serial"}, yes_no={"positions": False})
        assert cli.saved["MESHTASTIC_EXPOSE_POSITION"] == "false"

    def test_node_name_is_asked(self, cli):
        run(cli, answers={"transport": "serial", "bot node name": "Hermes"})
        assert cli.saved["MESHTASTIC_NODE_NAME"] == "Hermes"


class TestUnsupportedTransports:
    @pytest.mark.parametrize("value", ["ble", "mqtt"])
    def test_v1_unsupported_transports_are_refused(self, cli, value):
        run(cli, answers={"transport": value})
        assert "MESHTASTIC_TRANSPORT" not in cli.saved
        assert any("not supported in v1" in w for w in cli.warnings)

    def test_an_unknown_transport_is_refused(self, cli):
        run(cli, answers={"transport": "carrier-pigeon"})
        assert "MESHTASTIC_TRANSPORT" not in cli.saved
        assert any("unknown transport" in w.lower() for w in cli.warnings)


class TestWizardText:
    def test_it_tells_the_user_to_restart_the_gateway(self, cli):
        """The last step of the documented flow."""
        run(cli, answers={"transport": "serial"})
        assert any("hermes gateway restart" in m for m in cli.infos)

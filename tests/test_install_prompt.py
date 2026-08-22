"""Tests for the install-time network prompts.

The regression these cover, reported from a real install: Hermes prompts
for ``plugin.yaml``'s ``requires_env`` and not for ``optional_env``, so an
operator who answered ``tcp`` to the transport question was never asked for
the hostname or the port.  The install completed and the radio was
unreachable.

The prompts are simulated rather than driven through real stdin: the module
takes its ``prompt_fn``/``save_fn`` as arguments precisely so the questions
asked, in order, can be asserted on.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import pytest

import adapter as adapter_mod
import install_prompt
from envcheck import DEFAULT_TCP_PORT
from install_prompt import (
    MissingConfiguration,
    ensure_configured,
    pending_prompts,
    prompt_for_missing,
)


class FakePrompter:
    """Answers questions from a script and records what was asked.

    ``None`` in the script stands for pressing enter — the helper returns
    the default it was offered, which is what ``hermes_cli.setup.prompt``
    does.
    """

    def __init__(self, *answers: Optional[str]) -> None:
        self.answers: List[Optional[str]] = list(answers)
        self.asked: List[Tuple[str, str]] = []
        self.saved: Dict[str, str] = {}
        self.notices: List[str] = []

    def prompt(self, question: str, default: str = "") -> str:
        self.asked.append((question, default))
        if not self.answers:
            raise AssertionError(f"unscripted prompt: {question!r}")
        answer = self.answers.pop(0)
        return default if answer is None else answer

    def save(self, name: str, value: str) -> None:
        self.saved[name] = value

    def notify(self, message: str) -> None:
        self.notices.append(message)

    @property
    def questions(self) -> List[str]:
        return [question for question, _ in self.asked]

    def run(self, env: dict, *, ensure: bool = False) -> Dict[str, str]:
        runner = ensure_configured if ensure else prompt_for_missing
        return runner(
            env, prompt_fn=self.prompt, save_fn=self.save, notify=self.notify
        )


@pytest.fixture
def interactive(monkeypatch):
    """Pretend a human is at the keyboard.

    Under pytest stdin is not a tty, which is exactly the non-interactive
    branch — so the interactive tests have to say so explicitly.
    """
    monkeypatch.setattr(install_prompt, "_is_interactive", lambda: True)


def serial_env(**overrides: str) -> dict:
    base = {"MESHTASTIC_TRANSPORT": "serial", "MESHTASTIC_ALLOW_ALL_USERS": "false"}
    base.update(overrides)
    return base


def tcp_env(**overrides: str) -> dict:
    base = {"MESHTASTIC_TRANSPORT": "tcp", "MESHTASTIC_ALLOW_ALL_USERS": "false"}
    base.update(overrides)
    return base


def asked_about(prompter: FakePrompter, needle: str) -> bool:
    return any(needle in question.lower() for question in prompter.questions)


class TestTcpAsksForTheHost:
    """The exact gap the user hit: tcp chosen, host never requested."""

    def test_host_is_prompted_for(self, interactive):
        p = FakePrompter("radio.local", None)
        p.run(tcp_env())
        assert asked_about(p, "hostname")

    def test_the_answer_is_saved(self, interactive):
        p = FakePrompter("radio.local", None)
        p.run(tcp_env())
        assert p.saved["MESHTASTIC_TCP_HOST"] == "radio.local"

    def test_the_answer_is_returned(self, interactive):
        p = FakePrompter("radio.local", None)
        assert p.run(tcp_env())["MESHTASTIC_TCP_HOST"] == "radio.local"

    def test_surrounding_whitespace_is_trimmed(self, interactive):
        p = FakePrompter("  radio.local  ", None)
        p.run(tcp_env())
        assert p.saved["MESHTASTIC_TCP_HOST"] == "radio.local"

    def test_an_ip_address_is_accepted(self, interactive):
        p = FakePrompter("192.168.1.42", None)
        p.run(tcp_env())
        assert p.saved["MESHTASTIC_TCP_HOST"] == "192.168.1.42"


class TestTheHostIsMandatory:
    """Install must not complete without it — the prompt repeats."""

    def test_an_empty_answer_re_asks(self, interactive):
        p = FakePrompter("", "radio.local", None)
        p.run(tcp_env())
        assert p.questions.count(p.questions[0]) == 2

    def test_a_whitespace_only_answer_re_asks(self, interactive):
        p = FakePrompter("   ", "radio.local", None)
        p.run(tcp_env())
        assert p.saved["MESHTASTIC_TCP_HOST"] == "radio.local"

    def test_the_re_ask_explains_why(self, interactive):
        p = FakePrompter("", "radio.local", None)
        p.run(tcp_env())
        assert any("MESHTASTIC_TCP_HOST" in note for note in p.notices)

    def test_repeated_blanks_keep_asking_rather_than_giving_up(self, interactive):
        p = FakePrompter("", "", "", "radio.local", None)
        p.run(tcp_env())
        assert p.saved["MESHTASTIC_TCP_HOST"] == "radio.local"

    def test_a_blank_host_is_never_saved(self, interactive):
        p = FakePrompter("", "radio.local", None)
        p.run(tcp_env())
        assert p.saved["MESHTASTIC_TCP_HOST"].strip()


class TestTheTcpPortIsOfferedWithADefault:
    def test_the_port_is_prompted_for(self, interactive):
        p = FakePrompter("radio.local", None)
        p.run(tcp_env())
        assert asked_about(p, "port")

    def test_the_prompt_is_pre_populated_with_4403(self, interactive):
        p = FakePrompter("radio.local", None)
        p.run(tcp_env())
        defaults = {question: default for question, default in p.asked}
        port_question = next(q for q in p.questions if "port" in q.lower())
        assert defaults[port_question] == str(DEFAULT_TCP_PORT)

    def test_enter_accepts_the_default(self, interactive):
        p = FakePrompter("radio.local", None)
        p.run(tcp_env())
        assert p.saved["MESHTASTIC_TCP_PORT"] == str(DEFAULT_TCP_PORT)

    def test_a_custom_port_is_honoured(self, interactive):
        p = FakePrompter("radio.local", "14403")
        p.run(tcp_env())
        assert p.saved["MESHTASTIC_TCP_PORT"] == "14403"

    def test_the_port_is_asked_after_the_host(self, interactive):
        p = FakePrompter("radio.local", None)
        p.run(tcp_env())
        host_at = next(i for i, q in enumerate(p.questions) if "hostname" in q.lower())
        port_at = next(i for i, q in enumerate(p.questions) if "port" in q.lower())
        assert host_at < port_at


class TestThePortIsValidated:
    """A bad port is caught at the prompt, not at connect time."""

    @pytest.mark.parametrize("bad", ["abc", "44o3", "", " "])
    def test_non_numeric_is_rejected(self, interactive, bad):
        # "" and " " fall back to the pre-filled default rather than being
        # rejected — a port always has a working answer.
        p = FakePrompter("radio.local", bad, "4403")
        p.run(tcp_env())
        assert p.saved["MESHTASTIC_TCP_PORT"] == "4403"

    @pytest.mark.parametrize("bad", ["0", "-1", "65536", "99999"])
    def test_out_of_range_is_rejected_then_re_asked(self, interactive, bad):
        p = FakePrompter("radio.local", bad, "4403")
        p.run(tcp_env())
        assert p.saved["MESHTASTIC_TCP_PORT"] == "4403"
        assert any("MESHTASTIC_TCP_PORT" in note for note in p.notices)

    def test_the_rejection_names_the_range(self, interactive):
        p = FakePrompter("radio.local", "70000", "4403")
        p.run(tcp_env())
        assert any("65535" in note for note in p.notices)

    @pytest.mark.parametrize("ok", ["1", "4403", "8080", "65535"])
    def test_valid_ports_are_accepted_first_time(self, interactive, ok):
        p = FakePrompter("radio.local", ok)
        p.run(tcp_env())
        assert p.saved["MESHTASTIC_TCP_PORT"] == ok


class TestSerialAsksNeitherTcpQuestion:
    def test_no_host_question(self, interactive):
        p = FakePrompter()
        p.run(serial_env())
        assert not asked_about(p, "hostname")

    def test_no_port_question(self, interactive):
        p = FakePrompter()
        p.run(serial_env())
        assert not asked_about(p, "port")

    def test_nothing_at_all_is_asked(self, interactive):
        p = FakePrompter()
        assert p.run(serial_env()) == {}
        assert p.questions == []

    def test_nothing_tcp_is_saved(self, interactive):
        p = FakePrompter()
        p.run(serial_env())
        assert "MESHTASTIC_TCP_HOST" not in p.saved
        assert "MESHTASTIC_TCP_PORT" not in p.saved

    def test_the_serial_port_stays_optional(self, interactive):
        """Autodetect handles the single-radio case — never a blocker."""
        p = FakePrompter()
        p.run(serial_env())
        assert "MESHTASTIC_SERIAL_PORT" not in p.saved
        assert not asked_about(p, "serial")


class TestAlreadyConfiguredValuesAreNotRePrompted:
    """A pre-configured ~/.hermes/.env must not force interactivity."""

    def test_a_configured_host_is_not_asked_about(self, interactive):
        p = FakePrompter()
        p.run(tcp_env(MESHTASTIC_TCP_HOST="radio.local", MESHTASTIC_TCP_PORT="4403"))
        assert p.questions == []

    def test_a_configured_host_alone_still_offers_the_port(self, interactive):
        p = FakePrompter(None)
        p.run(tcp_env(MESHTASTIC_TCP_HOST="radio.local"))
        assert p.questions and asked_about(p, "port")
        assert not asked_about(p, "hostname")

    def test_a_configured_non_default_port_is_left_alone(self, interactive):
        p = FakePrompter()
        p.run(tcp_env(MESHTASTIC_TCP_HOST="radio.local", MESHTASTIC_TCP_PORT="14403"))
        assert p.questions == []
        assert p.saved == {}

    def test_a_blank_value_in_the_env_file_does_not_count_as_configured(
        self, interactive
    ):
        """``MESHTASTIC_TCP_HOST=`` is what a hand-written .env often holds."""
        p = FakePrompter("radio.local", None)
        p.run(tcp_env(MESHTASTIC_TCP_HOST="   "))
        assert asked_about(p, "hostname")

    def test_a_fully_configured_env_needs_no_prompter_at_all(self):
        """Not even the interactive fixture — nothing is asked either way."""
        p = FakePrompter()
        assert (
            p.run(
                tcp_env(MESHTASTIC_TCP_HOST="radio.local", MESHTASTIC_TCP_PORT="4403"),
                ensure=True,
            )
            == {}
        )


class TestNonInteractiveFailsClearlyRatherThanHanging:
    """Piped stdin, CI, ``--yes``: never block on input()."""

    def test_a_missing_host_raises(self):
        p = FakePrompter()
        with pytest.raises(MissingConfiguration):
            p.run(tcp_env())

    def test_nothing_was_asked(self):
        p = FakePrompter()
        with pytest.raises(MissingConfiguration):
            p.run(tcp_env())
        assert p.questions == []

    def test_the_message_names_the_variable(self):
        p = FakePrompter()
        with pytest.raises(MissingConfiguration) as e:
            p.run(tcp_env())
        assert "MESHTASTIC_TCP_HOST" in str(e.value)

    def test_the_message_gives_the_exact_env_line(self):
        p = FakePrompter()
        with pytest.raises(MissingConfiguration) as e:
            p.run(tcp_env())
        assert "MESHTASTIC_TCP_HOST=meshtastic.local" in str(e.value)
        assert "~/.hermes/.env" in str(e.value)

    def test_it_says_why_it_could_not_ask(self):
        p = FakePrompter()
        with pytest.raises(MissingConfiguration) as e:
            p.run(tcp_env())
        assert "not a terminal" in str(e.value)

    def test_the_optional_port_alone_does_not_fail_a_piped_install(self):
        """4403 is correct by construction — silence beats a hard failure."""
        p = FakePrompter()
        assert p.run(tcp_env(MESHTASTIC_TCP_HOST="radio.local")) == {}

    def test_serial_installs_non_interactively_with_no_complaint(self):
        p = FakePrompter()
        assert p.run(serial_env(), ensure=True) == {}


class TestPendingPrompts:
    """The question set is derived from ENV_RULES, not hand-listed."""

    def test_tcp_pends_both_network_questions(self):
        assert {r.name for r in pending_prompts(tcp_env())} == {
            "MESHTASTIC_TCP_HOST",
            "MESHTASTIC_TCP_PORT",
        }

    def test_serial_pends_nothing(self):
        assert pending_prompts(serial_env()) == []

    def test_every_pending_rule_has_a_question_to_ask(self):
        for rule in pending_prompts(tcp_env()):
            assert rule.prompt

    def test_only_the_port_carries_a_default(self):
        defaults = {r.name: r.default for r in pending_prompts(tcp_env())}
        assert defaults["MESHTASTIC_TCP_PORT"] == str(DEFAULT_TCP_PORT)
        assert defaults["MESHTASTIC_TCP_HOST"] == ""


class TestEnsureConfiguredCatchesWhatPromptingCannot:
    def test_an_unprompted_required_var_still_blocks(self):
        """ALLOW_ALL_USERS has no prompt here — Hermes asks for it."""
        with pytest.raises(MissingConfiguration) as e:
            FakePrompter().run({"MESHTASTIC_TRANSPORT": "serial"}, ensure=True)
        assert "MESHTASTIC_ALLOW_ALL_USERS" in str(e.value)

    def test_the_failure_gives_an_env_line(self):
        with pytest.raises(MissingConfiguration) as e:
            FakePrompter().run({"MESHTASTIC_TRANSPORT": "serial"}, ensure=True)
        assert "MESHTASTIC_ALLOW_ALL_USERS=false" in str(e.value)

    def test_a_host_supplied_at_the_prompt_satisfies_the_check(self, interactive):
        p = FakePrompter("radio.local", None)
        assert p.run(tcp_env(), ensure=True)["MESHTASTIC_TCP_HOST"] == "radio.local"


class TestTheAdapterInstallGate:
    """``complete_install_config`` is what an install path calls."""

    def test_it_prompts_and_succeeds(self, interactive, monkeypatch):
        p = FakePrompter("radio.local", None)
        monkeypatch.setenv("MESHTASTIC_TRANSPORT", "tcp")
        monkeypatch.setenv("MESHTASTIC_ALLOW_ALL_USERS", "false")
        assert (
            adapter_mod.complete_install_config(
                prompt_fn=p.prompt, save_fn=p.save, notify=p.notify
            )
            is True
        )
        assert p.saved["MESHTASTIC_TCP_HOST"] == "radio.local"

    def test_it_returns_false_and_logs_when_it_cannot_ask(self, monkeypatch, caplog):
        monkeypatch.setenv("MESHTASTIC_TRANSPORT", "tcp")
        monkeypatch.setenv("MESHTASTIC_ALLOW_ALL_USERS", "false")
        p = FakePrompter()
        with caplog.at_level("ERROR"):
            ready = adapter_mod.complete_install_config(
                prompt_fn=p.prompt, save_fn=p.save, notify=p.notify
            )
        assert ready is False
        assert "MESHTASTIC_TCP_HOST" in caplog.text

    def test_serial_needs_no_prompting(self, interactive, monkeypatch):
        monkeypatch.setenv("MESHTASTIC_TRANSPORT", "serial")
        monkeypatch.setenv("MESHTASTIC_ALLOW_ALL_USERS", "false")
        p = FakePrompter()
        assert (
            adapter_mod.complete_install_config(
                prompt_fn=p.prompt, save_fn=p.save, notify=p.notify
            )
            is True
        )
        assert p.questions == []

    def test_check_env_ready_can_still_be_a_pure_check(self, monkeypatch):
        monkeypatch.setenv("MESHTASTIC_TRANSPORT", "serial")
        monkeypatch.setenv("MESHTASTIC_ALLOW_ALL_USERS", "false")
        assert adapter_mod.check_env_ready(prompt=False) is True


class TestOneSourceOfTruth:
    """The rules are not duplicated between the wizard and the installer."""

    def test_the_prompter_reads_env_rules(self):
        import envcheck

        names = {rule.name for rule in envcheck.ENV_RULES}
        assert {r.name for r in pending_prompts(tcp_env())} <= names

    def test_the_wizard_delegates_the_tcp_questions(self):
        import inspect

        import setup_wizard

        source = inspect.getsource(setup_wizard.interactive_setup)
        assert "prompt_for_missing" in source
        # The questions must not also be hand-written a second time.
        assert "Radio hostname or IP" not in source

    def test_the_port_default_comes_from_envcheck(self):
        rule = next(
            r for r in pending_prompts(tcp_env()) if r.name == "MESHTASTIC_TCP_PORT"
        )
        assert rule.default == str(DEFAULT_TCP_PORT)


class TestAPortSuppliedAtInstallReachesTheConnection:
    """The value collected at the prompt must survive to TCPInterface."""

    def test_resolve_tcp_port_reads_what_was_saved(self, monkeypatch):
        from envcheck import resolve_tcp_port

        monkeypatch.setenv("MESHTASTIC_TCP_PORT", "14403")
        assert resolve_tcp_port() == 14403

    def test_the_adapter_carries_it(self, monkeypatch):
        from dataclasses import dataclass, field

        @dataclass
        class Cfg:
            enabled: bool = True
            extra: dict = field(default_factory=lambda: {"transport": "tcp"})

        monkeypatch.setenv("MESHTASTIC_TCP_PORT", "14403")
        assert adapter_mod.MeshtasticAdapter(Cfg()).tcp_port == 14403

    def test_the_transport_passes_it_to_tcp_interface(self, monkeypatch):
        import transport as tp

        captured = {}

        class FakeTCPInterface:
            def __init__(self, hostname=None, portNumber=None):
                captured["hostname"] = hostname
                captured["portNumber"] = portNumber

        module = type("m", (), {"TCPInterface": FakeTCPInterface})
        monkeypatch.setitem(
            __import__("sys").modules, "meshtastic.tcp_interface", module
        )
        tp._build_interface(transport="tcp", serial_port="", tcp_host="radio.local", tcp_port=14403)
        assert captured == {"hostname": "radio.local", "portNumber": 14403}

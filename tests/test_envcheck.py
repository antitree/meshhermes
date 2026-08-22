"""Tests for the conditional environment requirements.

The problem these cover: a flat list of install-time prompts cannot say
"required only when transport is tcp", so it can only under-ask (a tcp
install with no host, which fails at connect) or over-ask (a serial
operator made to supply a TCP host they do not have).

The plugin therefore prompts for nothing at install; ``hermes gateway
setup`` gathers everything, and these rules are the shared contract it and
the runtime both enforce.  That makes this module the safety net: a
gateway started before the wizard ever ran, or with a hand-written
``.env``, must still fail with a message naming the variable and the fix.

Every rule is checked in both directions: missing → reported with a message
that names the variable, and satisfied → passes with no complaint.  The
"satisfied" cases deliberately supply values through a plain mapping,
standing in for ``~/.hermes/.env``, to prove that a fully-configured env
file is accepted without any interaction at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict

import pytest

import adapter as adapter_mod
import envcheck
from envcheck import (
    DEFAULT_TCP_PORT,
    ENV_RULES,
    check_env,
    describe_requirements,
    resolve_tcp_port,
    validate_env,
)


@dataclass
class FakeConfig:
    """Stand-in for Hermes' PlatformConfig, as in test_adapter.py."""

    enabled: bool = True
    extra: Dict[str, Any] = field(default_factory=dict)


def env(**overrides: str) -> dict:
    """A minimally-valid serial configuration, plus *overrides*."""
    base = {
        "MESHTASTIC_TRANSPORT": "serial",
        "MESHTASTIC_ALLOW_ALL_USERS": "false",
    }
    base.update(overrides)
    return base


def names_of(problems) -> set:
    return {p.name for p in problems}


class TestSatisfiedConfigurations:
    """A complete env file passes — the no-prompting-needed case."""

    def test_minimal_serial_config_passes(self):
        assert validate_env(env()) is None

    def test_minimal_tcp_config_passes(self):
        assert (
            validate_env(
                env(MESHTASTIC_TRANSPORT="tcp", MESHTASTIC_TCP_HOST="meshtastic.local")
            )
            is None
        )

    def test_fully_populated_env_file_passes(self):
        """Everything set, as a thorough operator's .env would be."""
        assert (
            validate_env(
                env(
                    MESHTASTIC_TRANSPORT="tcp",
                    MESHTASTIC_TCP_HOST="10.0.0.5",
                    MESHTASTIC_TCP_PORT="4403",
                    MESHTASTIC_NODE_NAME="Hermes",
                    MESHTASTIC_ALLOWED_USERS="!aabbccdd",
                    MESHTASTIC_HOME_CHANNEL="channel:LongFast",
                    MESHTASTIC_EXPOSE_POSITION="false",
                    MESHTASTIC_AUTO_INSTALL="false",
                )
            )
            is None
        )

    def test_allow_all_true_is_a_valid_explicit_choice(self):
        assert validate_env(env(MESHTASTIC_ALLOW_ALL_USERS="true")) is None

    def test_empty_allowlist_with_allow_all_false_is_valid(self):
        """The wizard's 'pair a node later' path must stay supported."""
        assert (
            validate_env(env(MESHTASTIC_ALLOW_ALL_USERS="false", MESHTASTIC_ALLOWED_USERS=""))
            is None
        )


class TestOptionalVarsNeverBlock:
    """Unset optional vars must let the install proceed."""

    @pytest.mark.parametrize(
        "name",
        [
            "MESHTASTIC_SERIAL_PORT",
            "MESHTASTIC_TCP_PORT",
            "MESHTASTIC_ALLOWED_USERS",
            "MESHTASTIC_NODE_NAME",
            "MESHTASTIC_HOME_CHANNEL",
            "MESHTASTIC_EXPOSE_POSITION",
            "MESHTASTIC_AUTO_INSTALL",
        ],
    )
    def test_unset_optional_var_is_not_reported(self, name):
        assert name not in names_of(check_env(env()))

    def test_serial_without_a_port_is_fine(self):
        """Autodetect of a single attached radio is the common case."""
        assert validate_env(env(MESHTASTIC_SERIAL_PORT="")) is None


class TestTransportRequired:
    def test_missing_transport_blocks(self):
        problems = check_env({"MESHTASTIC_ALLOW_ALL_USERS": "false"})
        assert "MESHTASTIC_TRANSPORT" in names_of(problems)

    def test_message_names_the_variable_and_the_remedy(self):
        message = validate_env({"MESHTASTIC_ALLOW_ALL_USERS": "false"})
        assert "MESHTASTIC_TRANSPORT" in message
        # The operator needs both routes: the env file for a single value,
        # and the wizard, which is the canonical path — see
        # TestRemedyWording.
        assert ".env" in message
        assert "hermes gateway setup" in message

    def test_whitespace_only_counts_as_unset(self):
        """`MESHTASTIC_TRANSPORT=   ` in a .env must not satisfy the rule."""
        problems = check_env(env(MESHTASTIC_TRANSPORT="   "))
        assert "MESHTASTIC_TRANSPORT" in names_of(problems)

    @pytest.mark.parametrize("value", ["ble", "mqtt"])
    def test_v1_unsupported_transports_are_rejected_as_such(self, value):
        message = validate_env(env(MESHTASTIC_TRANSPORT=value))
        assert "not supported in v1" in message
        assert "ROADMAP" in message

    def test_unknown_transport_is_rejected(self):
        message = validate_env(env(MESHTASTIC_TRANSPORT="carrier-pigeon"))
        assert "unknown transport" in message

    @pytest.mark.parametrize("value", ["serial", "tcp"])
    def test_supported_transports_accepted(self, value):
        problems = check_env(env(MESHTASTIC_TRANSPORT=value, MESHTASTIC_TCP_HOST="h"))
        assert "MESHTASTIC_TRANSPORT" not in names_of(problems)


class TestTcpHostConditionallyRequired:
    """The headline bug: tcp with no host installed fine, then could not run."""

    def test_tcp_without_host_blocks(self):
        problems = check_env(env(MESHTASTIC_TRANSPORT="tcp"))
        assert "MESHTASTIC_TCP_HOST" in names_of(problems)

    def test_tcp_without_host_message_is_actionable(self):
        message = validate_env(env(MESHTASTIC_TRANSPORT="tcp"))
        assert "MESHTASTIC_TCP_HOST" in message
        assert "MESHTASTIC_TRANSPORT=tcp" in message
        assert "meshtastic.local" in message

    def test_serial_without_host_does_not_block(self):
        """The host is meaningless for serial and must not be demanded."""
        problems = check_env(env(MESHTASTIC_TRANSPORT="serial"))
        assert "MESHTASTIC_TCP_HOST" not in names_of(problems)

    def test_tcp_with_host_from_env_file_passes(self):
        assert (
            validate_env(env(MESHTASTIC_TRANSPORT="tcp", MESHTASTIC_TCP_HOST="radio.local"))
            is None
        )

    def test_blank_host_does_not_satisfy_the_rule(self):
        problems = check_env(env(MESHTASTIC_TRANSPORT="tcp", MESHTASTIC_TCP_HOST="  "))
        assert "MESHTASTIC_TCP_HOST" in names_of(problems)


class TestAccessControlMustBeExplicit:
    def test_unset_allow_all_is_reported(self):
        problems = check_env({"MESHTASTIC_TRANSPORT": "serial"})
        assert "MESHTASTIC_ALLOW_ALL_USERS" in names_of(problems)

    def test_message_offers_both_choices(self):
        message = validate_env({"MESHTASTIC_TRANSPORT": "serial"})
        assert "MESHTASTIC_ALLOW_ALL_USERS=false" in message
        assert "MESHTASTIC_ALLOW_ALL_USERS=true" in message
        # The pairing workflow must remain visibly available.
        assert "pair a node later" in message

    def test_an_allowlist_alone_does_not_imply_the_decision(self):
        """Listing nodes is not the same as answering the open/closed question."""
        problems = check_env(
            {"MESHTASTIC_TRANSPORT": "serial", "MESHTASTIC_ALLOWED_USERS": "!aabbccdd"}
        )
        assert "MESHTASTIC_ALLOW_ALL_USERS" in names_of(problems)

    def test_non_boolean_value_is_rejected(self):
        problems = check_env(env(MESHTASTIC_ALLOW_ALL_USERS="maybe"))
        assert "MESHTASTIC_ALLOW_ALL_USERS" in names_of(problems)
        assert problems[0].kind == "invalid"

    @pytest.mark.parametrize("value", ["true", "false", "1", "0", "yes", "no", "on", "off"])
    def test_accepted_boolean_spellings(self, value):
        problems = check_env(env(MESHTASTIC_ALLOW_ALL_USERS=value))
        assert "MESHTASTIC_ALLOW_ALL_USERS" not in names_of(problems)

    def test_it_is_an_install_scope_rule_only(self):
        """Runtime must not re-enforce it and lock out a working deployment."""
        source = {"MESHTASTIC_TRANSPORT": "serial"}
        assert "MESHTASTIC_ALLOW_ALL_USERS" in names_of(check_env(source, scope="install"))
        assert "MESHTASTIC_ALLOW_ALL_USERS" not in names_of(
            check_env(source, scope="runtime")
        )


class TestScopes:
    def test_tcp_host_blocks_in_both_scopes(self):
        """A missing host makes the radio unreachable however you got there."""
        source = env(MESHTASTIC_TRANSPORT="tcp")
        for scope in ("install", "runtime"):
            assert "MESHTASTIC_TCP_HOST" in names_of(check_env(source, scope=scope))

    def test_invalid_values_are_reported_in_runtime_scope_too(self):
        problems = check_env(env(MESHTASTIC_TRANSPORT="ble"), scope="runtime")
        assert "MESHTASTIC_TRANSPORT" in names_of(problems)


class TestTcpPortValidation:
    def test_unset_port_is_optional(self):
        assert validate_env(env(MESHTASTIC_TRANSPORT="tcp", MESHTASTIC_TCP_HOST="h")) is None

    def test_non_numeric_port_is_rejected(self):
        problems = check_env(env(MESHTASTIC_TCP_PORT="not-a-port"))
        assert "MESHTASTIC_TCP_PORT" in names_of(problems)

    @pytest.mark.parametrize("value", ["0", "65536", "-1"])
    def test_out_of_range_port_is_rejected(self, value):
        problems = check_env(env(MESHTASTIC_TCP_PORT=value))
        assert "MESHTASTIC_TCP_PORT" in names_of(problems)

    def test_valid_port_accepted(self):
        problems = check_env(env(MESHTASTIC_TCP_PORT="8080"))
        assert "MESHTASTIC_TCP_PORT" not in names_of(problems)


class TestResolveTcpPort:
    def test_defaults_to_4403(self):
        assert resolve_tcp_port({}) == DEFAULT_TCP_PORT

    def test_env_value_wins(self):
        assert resolve_tcp_port({"MESHTASTIC_TCP_PORT": "9999"}) == 9999

    def test_falls_back_to_config_extra(self):
        assert resolve_tcp_port({}, extra={"tcp_port": 5555}) == 5555

    def test_env_beats_config_extra(self):
        assert resolve_tcp_port({"MESHTASTIC_TCP_PORT": "1234"}, extra={"tcp_port": 5555}) == 1234

    @pytest.mark.parametrize("value", ["junk", "0", "70000"])
    def test_unusable_values_fall_back_to_the_default(self, value):
        """check_env already reports it; refusing to connect twice helps nobody."""
        assert resolve_tcp_port({"MESHTASTIC_TCP_PORT": value}) == DEFAULT_TCP_PORT


class TestSeveralProblemsAtOnce:
    def test_all_are_reported_not_just_the_first(self):
        problems = check_env({"MESHTASTIC_TRANSPORT": "tcp"})
        assert names_of(problems) == {"MESHTASTIC_TCP_HOST", "MESHTASTIC_ALLOW_ALL_USERS"}

    def test_summary_counts_them(self):
        message = validate_env({"MESHTASTIC_TRANSPORT": "tcp"})
        assert "2 problems" in message


class TestRuleSetShape:
    """The rule set is the contract other modules rely on."""

    def test_every_rule_has_a_name_and_a_summary(self):
        for rule in ENV_RULES:
            assert rule.name.startswith("MESHTASTIC_")
            assert rule.summary

    def test_every_blocking_rule_carries_a_remedy(self):
        for rule in ENV_RULES:
            if rule.required_when({"MESHTASTIC_TRANSPORT": "tcp"}):
                assert rule.remedy, f"{rule.name} can block but suggests no fix"

    def test_rule_names_are_unique(self):
        names = [rule.name for rule in ENV_RULES]
        assert len(names) == len(set(names))

    def test_scopes_are_known_values(self):
        assert {rule.scope for rule in ENV_RULES} <= {"install", "runtime"}

    def test_describe_requirements_lists_only_blockers(self):
        described = " ".join(describe_requirements())
        assert "MESHTASTIC_TRANSPORT" in described
        assert "MESHTASTIC_TCP_HOST" in described
        # Optional vars must not appear as requirements.
        assert "MESHTASTIC_NODE_NAME" not in described
        assert "MESHTASTIC_HOME_CHANNEL" not in described


class TestReadsTheProcessEnvironmentByDefault:
    def test_env_file_values_loaded_into_environ_satisfy_the_rules(self, monkeypatch):
        """Hermes loads ~/.hermes/.env into os.environ before the plugin runs."""
        monkeypatch.setenv("MESHTASTIC_TRANSPORT", "tcp")
        monkeypatch.setenv("MESHTASTIC_TCP_HOST", "radio.local")
        monkeypatch.setenv("MESHTASTIC_ALLOW_ALL_USERS", "false")
        assert validate_env() is None

    def test_missing_host_in_the_process_env_blocks(self, monkeypatch):
        monkeypatch.setenv("MESHTASTIC_TRANSPORT", "tcp")
        monkeypatch.setenv("MESHTASTIC_ALLOW_ALL_USERS", "false")
        assert "MESHTASTIC_TCP_HOST" in (validate_env() or "")


class TestCheckEnvReady:
    """``check_env_ready`` is a pure predicate over the whole rule set.

    It is no longer an install gate — installing prompts for nothing and
    blocks on nothing.  It survives as a non-prompting "is this env file
    coherent?" check that logs what is wrong.
    """

    def test_passes_on_a_complete_env(self, monkeypatch):
        monkeypatch.setenv("MESHTASTIC_TRANSPORT", "serial")
        monkeypatch.setenv("MESHTASTIC_ALLOW_ALL_USERS", "false")
        assert adapter_mod.check_env_ready() is True

    def test_reports_tcp_without_a_host(self, monkeypatch):
        monkeypatch.setenv("MESHTASTIC_TRANSPORT", "tcp")
        monkeypatch.setenv("MESHTASTIC_ALLOW_ALL_USERS", "false")
        assert adapter_mod.check_env_ready() is False

    def test_reports_undecided_access_control(self, monkeypatch):
        monkeypatch.setenv("MESHTASTIC_TRANSPORT", "serial")
        assert adapter_mod.check_env_ready() is False

    def test_logs_the_reason(self, monkeypatch, caplog):
        monkeypatch.setenv("MESHTASTIC_TRANSPORT", "tcp")
        monkeypatch.setenv("MESHTASTIC_ALLOW_ALL_USERS", "false")
        with caplog.at_level("ERROR"):
            adapter_mod.check_env_ready()
        assert "MESHTASTIC_TCP_HOST" in caplog.text


class TestConfigYamlSatisfiesRequirements:
    """An operator who configured config.yaml must not be told it is missing."""

    def test_extra_supplies_the_tcp_host(self):
        resolved = adapter_mod._effective_env({"transport": "tcp", "tcp_host": "radio.local"})
        assert validate_env(resolved, scope="runtime") is None

    def test_extra_without_a_host_still_blocks(self):
        resolved = adapter_mod._effective_env({"transport": "tcp"})
        assert "MESHTASTIC_TCP_HOST" in (validate_env(resolved, scope="runtime") or "")

    def test_env_overrides_config_extra(self, monkeypatch):
        monkeypatch.setenv("MESHTASTIC_TRANSPORT", "serial")
        resolved = adapter_mod._effective_env({"transport": "tcp"})
        assert resolved["MESHTASTIC_TRANSPORT"] == "serial"


class TestAdapterConnectEnforcement:
    """A misconfigured install must fail at connect with a usable message."""

    async def test_tcp_without_host_fails_before_touching_the_radio(self):
        a = adapter_mod.MeshtasticAdapter(FakeConfig(extra={"transport": "tcp"}))
        assert await a.connect() is False
        assert a.has_fatal_error
        assert a.fatal_error_code == "config_missing"
        assert "MESHTASTIC_TCP_HOST" in a.fatal_error_message

    async def test_the_failure_is_not_retryable(self):
        """A missing variable will not fix itself; retrying just spins."""
        a = adapter_mod.MeshtasticAdapter(FakeConfig(extra={"transport": "tcp"}))
        await a.connect()
        assert a.fatal_error_retryable is False

    async def test_undecided_access_control_does_not_block_connect(self, monkeypatch):
        """Install-scope only — an upgraded deployment must keep working."""
        monkeypatch.setenv("MESHTASTIC_TRANSPORT", "serial")
        a = adapter_mod.MeshtasticAdapter(FakeConfig(extra={"transport": "serial"}))
        problem = envcheck.validate_env(
            adapter_mod._effective_env(a._extra), scope="runtime"
        )
        assert problem is None


class TestValidateConfigUsesTheSharedRules:
    def test_tcp_without_host_is_invalid(self):
        assert adapter_mod.validate_config(FakeConfig(extra={"transport": "tcp"})) is False

    def test_tcp_with_host_is_valid(self):
        assert (
            adapter_mod.validate_config(
                FakeConfig(extra={"transport": "tcp", "tcp_host": "radio.local"})
            )
            is True
        )

    @pytest.mark.parametrize("value", ["ble", "mqtt", "nonsense"])
    def test_bad_transports_rejected(self, value):
        assert adapter_mod.validate_config(FakeConfig(extra={"transport": value})) is False

    def test_serial_without_a_port_is_valid(self):
        assert adapter_mod.validate_config(FakeConfig(extra={"transport": "serial"})) is True


class TestRemedyWording:
    """The docs and every user-facing string tell one install story.

    README.md documents the canonical flow as ``hermes plugins install``
    -> enable the plugin -> ``hermes gateway setup`` -> ``hermes gateway
    restart``.  The install itself asks nothing, so the wizard is now a
    required step rather than an optional reconfigure — and every rule's
    remedy must name it, because it is the path that actually fixes a
    missing variable.

    The remedies still lead with ``~/.hermes/.env``: a operator staring at
    one broken variable should be able to fix that one variable without
    walking a whole wizard.  Both routes, wizard named second.
    """

    def blocking_rules(self):
        return [
            rule
            for rule in ENV_RULES
            if rule.required_when({"MESHTASTIC_TRANSPORT": "tcp"})
            or rule.required_when({"MESHTASTIC_TRANSPORT": "serial"})
        ]

    def test_every_remedy_names_the_wizard(self):
        """The wizard is the canonical configuration path, so say so."""
        for rule in self.blocking_rules():
            assert "hermes gateway setup" in rule.remedy, rule.name

    def test_remedies_lead_with_the_env_file(self):
        """Setting the value directly is the first-class answer.

        The wizard writes ``~/.hermes/.env``; an operator fixing one value
        should see that same surface named, not be sent through a whole
        wizard for a single setting.
        """
        for rule in self.blocking_rules():
            assert "~/.hermes/.env" in rule.remedy, rule.name

    def test_no_remedy_tells_the_operator_to_install_the_plugin_again(self):
        """A missing variable is never fixed by re-running the installer."""
        for rule in self.blocking_rules():
            assert "plugins install" not in rule.remedy, rule.name

    def test_transport_error_for_a_missing_tcp_host_matches_the_rule(self):
        """transport.py's own message must not drift from ENV_RULES."""
        pytest.importorskip("meshtastic.tcp_interface")
        import transport as transport_mod

        with pytest.raises(transport_mod.TransportError) as excinfo:
            transport_mod._build_interface("tcp", serial_port="", tcp_host="")

        message = str(excinfo.value)
        assert "MESHTASTIC_TCP_HOST" in message
        assert "~/.hermes/.env" in message
        assert "hermes gateway setup" in message


class TestRuntimeSafetyNetSurvivesTheQuietInstall:
    """Losing install-time prompting must not mean starting broken silently.

    The install now asks nothing at all, so a gateway can be started with a
    configuration nobody ever answered a question about. That makes these
    the last line of defence: they must fail loudly, early, and name both
    the variable and the fix.
    """

    async def test_tcp_without_host_fails_the_gateway_start(self):
        a = adapter_mod.MeshtasticAdapter(FakeConfig(extra={"transport": "tcp"}))
        assert await a.connect() is False
        assert a.has_fatal_error

    async def test_the_failure_names_the_variable_and_the_fix(self):
        a = adapter_mod.MeshtasticAdapter(FakeConfig(extra={"transport": "tcp"}))
        await a.connect()
        message = a.fatal_error_message
        assert "MESHTASTIC_TCP_HOST" in message
        assert "hermes gateway setup" in message
        assert "~/.hermes/.env" in message

    async def test_the_failure_is_categorised_as_configuration_not_hardware(self):
        """A user must not go hunting for a radio fault."""
        a = adapter_mod.MeshtasticAdapter(FakeConfig(extra={"transport": "tcp"}))
        await a.connect()
        assert a.fatal_error_code == "config_missing"
        assert a.fatal_error_retryable is False

    async def test_an_entirely_unconfigured_gateway_fails_on_the_transport(self):
        """Nothing answered at install, nothing run afterwards."""
        a = adapter_mod.MeshtasticAdapter(FakeConfig(extra={}))
        assert await a.connect() is False
        assert "MESHTASTIC_TRANSPORT" in a.fatal_error_message

    def test_validate_config_rejects_tcp_without_a_host(self):
        """The pre-start check catches it too, not only connect()."""
        assert adapter_mod.validate_config(FakeConfig(extra={"transport": "tcp"})) is False

    async def test_a_complete_tcp_config_gets_past_the_env_gate(self, monkeypatch):
        """The net must not fire on a configuration the wizard would produce."""
        monkeypatch.setenv("MESHTASTIC_TRANSPORT", "tcp")
        monkeypatch.setenv("MESHTASTIC_TCP_HOST", "radio.local")
        monkeypatch.setenv("MESHTASTIC_ALLOW_ALL_USERS", "false")
        a = adapter_mod.MeshtasticAdapter(FakeConfig(extra={}))
        problem = envcheck.validate_env(
            adapter_mod._effective_env(a._extra), scope="runtime"
        )
        assert problem is None

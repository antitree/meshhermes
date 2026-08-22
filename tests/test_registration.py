"""Tests for plugin registration and the manifest.

These verify the plugin presents itself to Hermes correctly — the part that
determines whether it loads at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

import adapter as adapter_mod
import mesh_tools

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


class FakeCtx:
    """Records what the plugin registers, standing in for PluginContext."""

    def __init__(self) -> None:
        self.platforms: List[Dict[str, Any]] = []
        self.tools: List[Dict[str, Any]] = []

    def register_platform(self, **kwargs: Any) -> None:
        self.platforms.append(kwargs)

    def register_tool(self, **kwargs: Any) -> None:
        self.tools.append(kwargs)


@pytest.fixture
def registered() -> FakeCtx:
    ctx = FakeCtx()
    adapter_mod.register(ctx)
    return ctx


class TestPlatformRegistration:
    def test_registers_exactly_one_platform(self, registered):
        assert len(registered.platforms) == 1

    def test_core_identity(self, registered):
        entry = registered.platforms[0]
        assert entry["name"] == "meshtastic"
        assert entry["label"] == "Meshtastic"
        # The gateway's icon: a bunny rabbit, matching plugin.yaml's `icon:`.
        assert entry["emoji"] == "🐰"

    def test_factory_builds_an_adapter(self, registered):
        from dataclasses import dataclass, field

        @dataclass
        class Cfg:
            enabled: bool = True
            extra: dict = field(default_factory=lambda: {"transport": "serial"})

        adapter = registered.platforms[0]["adapter_factory"](Cfg())
        assert isinstance(adapter, adapter_mod.MeshtasticAdapter)

    def test_hooks_are_wired(self, registered):
        entry = registered.platforms[0]
        # Each of these silently disables a feature if omitted.
        assert entry["check_fn"] is adapter_mod.check_requirements
        assert entry["validate_config"] is adapter_mod.validate_config
        assert entry["is_connected"] is adapter_mod.is_connected
        assert entry["env_enablement_fn"] is adapter_mod._env_enablement
        assert entry["standalone_sender_fn"] is adapter_mod._standalone_send
        assert callable(entry["setup_fn"])

    def test_registration_declares_no_required_env(self, registered):
        """The other install-prompt surface, and it must stay empty too.

        ``required_env`` on the registration is the same flat list as
        ``plugin.yaml``'s ``requires_env`` and has the same defect. Leaving
        it out is what keeps the install silent even if the manifest were
        read differently.
        """
        entry = registered.platforms[0]
        assert not entry.get("required_env")

    def test_setup_fn_is_the_wizard(self, registered):
        """`hermes gateway setup` is the canonical configuration path.

        It is also the only configuration hook whose invocation by Hermes
        we can actually verify, which is why the plugin routes everything
        through it rather than an install-time hook.
        """
        assert registered.platforms[0]["setup_fn"] is adapter_mod._interactive_setup

    def test_cron_and_auth_env_vars(self, registered):
        entry = registered.platforms[0]
        assert entry["cron_deliver_env_var"] == "MESHTASTIC_HOME_CHANNEL"
        assert entry["allowed_users_env"] == "MESHTASTIC_ALLOWED_USERS"
        assert entry["allow_all_env"] == "MESHTASTIC_ALLOW_ALL_USERS"

    def test_message_length_matches_lora_frame(self, registered):
        assert registered.platforms[0]["max_message_length"] == 200

    def test_platform_hint_tells_the_model_about_lora(self, registered):
        hint = registered.platforms[0]["platform_hint"].lower()
        assert "lora" in hint or "radio" in hint
        assert "concise" in hint or "brevity" in hint
        assert "markdown" in hint

    def test_install_hint_present(self, registered):
        assert "pip install meshtastic" in registered.platforms[0]["install_hint"]


class TestToolRegistration:
    def test_all_four_tools_registered(self, registered):
        names = {t["name"] for t in registered.tools}
        assert names == {"mesh_nodes", "mesh_telemetry", "mesh_channels", "mesh_send"}

    def test_handlers_are_callable_and_match(self, registered):
        by_name = {t["name"]: t for t in registered.tools}
        assert by_name["mesh_nodes"]["handler"] is mesh_tools.mesh_nodes_handler
        assert by_name["mesh_send"]["handler"] is mesh_tools.mesh_send_handler

    def test_schemas_have_required_shape(self, registered):
        for tool in registered.tools:
            schema = tool["schema"]
            assert schema["name"] == tool["name"]
            assert schema["description"]
            assert schema["input_schema"]["type"] == "object"

    def test_send_schema_warns_about_airtime(self, registered):
        send = next(t for t in registered.tools if t["name"] == "mesh_send")
        description = send["schema"]["description"].lower()
        # The model must understand this is a real radio, not a chat API.
        assert "radio" in description
        assert "airtime" in description

    def test_send_schema_requires_target_and_message(self, registered):
        send = next(t for t in registered.tools if t["name"] == "mesh_send")
        assert set(send["schema"]["input_schema"]["required"]) == {"target", "message"}


class TestManifest:
    @pytest.fixture
    def manifest(self) -> Dict[str, Any]:
        return yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text())

    def test_declares_platform_kind(self, manifest):
        # Anything else and the gateway will not load it as a platform.
        assert manifest["kind"] == "platform"

    def test_name_follows_ecosystem_convention(self, manifest):
        assert manifest["name"] == "meshtastic-platform"

    def test_declares_no_mandatory_install_prompts(self, manifest):
        """The install must ask nothing.

        Hermes' generic installer prompts for whatever it finds in
        ``requires_env``.  A flat list cannot say "a TCP host is mandatory,
        but only once you have chosen tcp", so it can only under-ask or
        over-ask.  Configuration lives in ``hermes gateway setup`` instead,
        and the manifest must not resurrect a prompt list.

        The key is *omitted*, not empty: an empty YAML value parses as
        ``None``, which a loader iterating it would trip over.
        """
        assert "requires_env" not in manifest
        assert manifest.get("requires_env") is None

    def test_every_variable_the_plugin_reads_is_documented(self, manifest):
        """Moving out of requires_env must not lose the documentation.

        Every rule in ``envcheck.ENV_RULES`` — including the two that used
        to be prompted for — has to remain described in ``optional_env``.
        """
        import envcheck

        names = {e["name"] for e in manifest["optional_env"]}
        assert {rule.name for rule in envcheck.ENV_RULES} <= names
        # The two that PR #1 promoted into requires_env, explicitly.
        assert {"MESHTASTIC_TRANSPORT", "MESHTASTIC_ALLOW_ALL_USERS"} <= names
        assert {"MESHTASTIC_SERIAL_PORT", "MESHTASTIC_TCP_HOST",
                "MESHTASTIC_HOME_CHANNEL", "MESHTASTIC_EXPOSE_POSITION"} <= names

    def test_icon_is_a_bunny(self, manifest):
        """The gateway's icon, and it must match what register() passes."""
        assert manifest["icon"] == "🐰"

    def test_icon_is_a_short_bare_emoji(self, manifest):
        """No markup, no path — the convention elsewhere is a bare emoji."""
        icon = manifest["icon"]
        assert isinstance(icon, str)
        assert 0 < len(icon) <= 4
        assert "<" not in icon and "/" not in icon

    def test_icon_matches_the_registered_emoji(self, manifest, registered):
        """One icon, two surfaces — they must not drift apart."""
        assert registered.platforms[0]["emoji"] == manifest["icon"]

    def test_manifest_still_parses_with_the_icon_key(self, manifest):
        """A guard that the added key did not break the document."""
        assert manifest["kind"] == "platform"
        assert manifest["name"] == "meshtastic-platform"

    def test_every_env_entry_is_fully_described(self, manifest):
        for key in ("requires_env", "optional_env"):
            for entry in manifest.get(key, []):
                assert entry.get("name") and entry.get("description") and entry.get("prompt")

    def test_entry_point_exists(self):
        assert (PLUGIN_ROOT / "__init__.py").exists()


class TestEnvEnablement:
    def test_returns_none_without_transport(self):
        assert adapter_mod._env_enablement() is None

    def test_seeds_from_env(self, monkeypatch):
        monkeypatch.setenv("MESHTASTIC_TRANSPORT", "tcp")
        monkeypatch.setenv("MESHTASTIC_TCP_HOST", "radio.local")
        monkeypatch.setenv("MESHTASTIC_HOME_CHANNEL", "channel:LongFast")
        seed = adapter_mod._env_enablement()
        assert seed["transport"] == "tcp"
        assert seed["tcp_host"] == "radio.local"
        # home_channel is turned into a HomeChannel by the core hook.
        assert seed["home_channel"]["chat_id"] == "channel:LongFast"


class TestPostInstallGuidance:
    """The install asks nothing, so it must say what to do next.

    Whether Hermes surfaces this string during ``hermes plugins install``
    is not something this repo can prove — there is no verified post-install
    hook. README.md is the guaranteed source of truth. These tests pin the
    text the plugin exposes so the two cannot disagree.
    """

    def test_message_names_the_three_steps_in_order(self):
        message = adapter_mod.post_install_message()
        lowered = message.lower()
        for step in ("enable", "hermes gateway setup", "hermes gateway restart"):
            assert step in lowered
        assert lowered.index("enable") < lowered.index("hermes gateway setup")
        assert lowered.index("hermes gateway setup") < lowered.index(
            "hermes gateway restart"
        )

    def test_message_is_a_plain_string_constant(self):
        assert adapter_mod.post_install_message() is adapter_mod.POST_INSTALL_MESSAGE
        assert isinstance(adapter_mod.POST_INSTALL_MESSAGE, str)

    def test_it_does_not_tell_the_user_to_answer_prompts(self):
        """The old flow's wording must not survive here."""
        lowered = adapter_mod.POST_INSTALL_MESSAGE.lower()
        assert "prompt" not in lowered
        assert "answer" not in lowered

    def test_producing_it_prompts_for_nothing(self, monkeypatch):
        """Calling it must never read stdin — install is non-interactive."""
        def explode(*args, **kwargs):
            raise AssertionError("post-install guidance must not prompt")

        monkeypatch.setattr("builtins.input", explode)
        assert adapter_mod.post_install_message()

    def test_readme_documents_the_same_three_steps(self):
        """README is the reliable surface, so it must carry the flow."""
        readme = (PLUGIN_ROOT / "README.md").read_text()
        assert "hermes plugins install antitree/meshhermes" in readme
        assert "hermes gateway setup" in readme
        assert "hermes gateway restart" in readme


class TestCheckEnvReadyIsAPureCheck:
    """It survives as a predicate, not as an interactive install gate."""

    def test_it_never_prompts(self, monkeypatch):
        def explode(*args, **kwargs):
            raise AssertionError("check_env_ready must not prompt")

        monkeypatch.setattr("builtins.input", explode)
        monkeypatch.setenv("MESHTASTIC_TRANSPORT", "tcp")
        monkeypatch.delenv("MESHTASTIC_TCP_HOST", raising=False)
        assert adapter_mod.check_env_ready() is False

    def test_it_is_not_wired_into_registration(self, registered):
        """Nothing hands Hermes an install-time gate to call."""
        entry = registered.platforms[0]
        assert adapter_mod.check_env_ready not in entry.values()

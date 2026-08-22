"""Tests for plugin registration and the manifest.

These verify the plugin presents itself to Hermes correctly — the part that
determines whether it loads at all.
"""

from __future__ import annotations

import re
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
        assert entry["emoji"] == "📻"

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

    def test_manifest_name_matches_runtime_registration(self, manifest, registered):
        # The manifest and ctx.register_platform() must agree.  They did not
        # historically — the manifest said "meshtastic-platform" while the
        # runtime registered "meshtastic" — and the runtime name is the one
        # Hermes routes `platforms: meshtastic:` config on.
        assert manifest["name"] == "meshtastic"
        assert manifest["name"] == registered.platforms[0]["name"]

    def test_manifest_label_matches_runtime_registration(self, manifest, registered):
        assert manifest["label"] == "Meshtastic"
        assert manifest["label"] == registered.platforms[0]["label"]

    def test_entry_point_name_is_left_unrenamed(self):
        # Deliberately NOT "meshtastic": this identifier is what existing
        # installs carry in ~/.hermes/config.yaml under `plugins: enabled:`,
        # so renaming it would strand them.  A bare `meshtastic` directory on
        # sys.path would also shadow the pip package adapter.py imports.
        text = (PLUGIN_ROOT / "pyproject.toml").read_text()
        assert 'meshtastic-platform = "meshhermes"' in text

    def test_requires_transport_env(self, manifest):
        names = {e["name"] for e in manifest["requires_env"]}
        assert "MESHTASTIC_TRANSPORT" in names

    def test_optional_env_documented(self, manifest):
        names = {e["name"] for e in manifest["optional_env"]}
        assert {"MESHTASTIC_SERIAL_PORT", "MESHTASTIC_TCP_HOST",
                "MESHTASTIC_HOME_CHANNEL", "MESHTASTIC_EXPOSE_POSITION"} <= names

    def test_every_env_entry_is_fully_described(self, manifest):
        for key in ("requires_env", "optional_env"):
            for entry in manifest.get(key, []):
                assert entry.get("name") and entry.get("description") and entry.get("prompt")

    def test_entry_point_exists(self):
        assert (PLUGIN_ROOT / "__init__.py").exists()


class TestVersionConsistency:
    """The version is declared in three files that nothing keeps in sync.

    A drifting version is invisible until a release is cut with the wrong
    number, so assert the three agree rather than trusting a release
    checklist.  CI runs the equivalent check; having it here means a local
    `pytest` catches the drift before the push.
    """

    @staticmethod
    def _pyproject_version() -> str:
        text = (PLUGIN_ROOT / "pyproject.toml").read_text()
        m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
        assert m, "no version in pyproject.toml"
        return m.group(1)

    @staticmethod
    def _manifest_version() -> str:
        manifest = yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text())
        return str(manifest["version"])

    @staticmethod
    def _init_version() -> str:
        text = (PLUGIN_ROOT / "__init__.py").read_text()
        m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
        assert m, "no __version__ in __init__.py"
        return m.group(1)

    def test_all_three_declarations_agree(self):
        versions = {
            "pyproject.toml": self._pyproject_version(),
            "plugin.yaml": self._manifest_version(),
            "__init__.py": self._init_version(),
        }
        assert len(set(versions.values())) == 1, f"version mismatch: {versions}"

    def test_version_is_a_semver_triple(self):
        assert re.fullmatch(r"\d+\.\d+\.\d+", self._pyproject_version())

    def test_changelog_documents_the_current_version(self):
        changelog = (PLUGIN_ROOT / "CHANGELOG.md").read_text()
        assert f"## [{self._pyproject_version()}]" in changelog


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

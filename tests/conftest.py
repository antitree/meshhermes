"""Test configuration.

The plugin loads as a package rooted at the plugin directory (Hermes sets
``__path__`` to the plugin dir), so at runtime the modules import each other
relatively.  Under pytest there is no such package, so the plugin root goes
on ``sys.path`` and the tests import the modules top-level.

``gateway.*`` is stubbed when the real Hermes package is not importable, so
the suite runs standalone in CI with neither Hermes nor a radio attached.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _path in (str(PLUGIN_ROOT), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


def _install_gateway_stubs() -> None:
    """Provide the minimal ``gateway`` surface ``adapter.py`` imports.

    Only used when the real Hermes package is absent.  The stub mirrors the
    real contract closely enough that the adapter's logic — dispatch,
    chunking, state reporting — is genuinely exercised; it is not a mock of
    the adapter itself.
    """
    try:
        import gateway.platforms.base  # noqa: F401
        import gateway.config  # noqa: F401

        return  # real Hermes available; use it
    except Exception:
        pass

    from dataclasses import dataclass, field
    from enum import Enum
    from typing import Any, List, Optional

    gateway = types.ModuleType("gateway")
    gateway.__path__ = []  # type: ignore[attr-defined]

    # -- gateway.config --
    config_mod = types.ModuleType("gateway.config")

    class Platform(Enum):
        LOCAL = "local"

        @classmethod
        def _missing_(cls, value):
            if not isinstance(value, str) or not value.strip():
                return None
            value = value.strip().lower()
            if value in cls._value2member_map_:
                return cls._value2member_map_[value]
            pseudo = object.__new__(cls)
            pseudo._name_ = value.upper()
            pseudo._value_ = value
            cls._value2member_map_[value] = pseudo
            return pseudo

    @dataclass
    class PlatformConfig:
        enabled: bool = False
        token: Optional[str] = None
        extra: dict = field(default_factory=dict)

    config_mod.Platform = Platform
    config_mod.PlatformConfig = PlatformConfig

    # -- gateway.session --
    session_mod = types.ModuleType("gateway.session")

    @dataclass
    class SessionSource:
        platform: str = ""
        chat_id: str = ""
        chat_name: str = ""
        chat_type: str = ""
        user_id: str = ""
        user_name: str = ""

    session_mod.SessionSource = SessionSource

    # -- gateway.platforms.base --
    platforms_mod = types.ModuleType("gateway.platforms")
    platforms_mod.__path__ = []  # type: ignore[attr-defined]
    base_mod = types.ModuleType("gateway.platforms.base")

    class MessageType(Enum):
        TEXT = "text"

    @dataclass
    class MessageEvent:
        text: str = ""
        message_type: Any = MessageType.TEXT
        source: Any = None
        raw_message: Any = None
        message_id: Optional[str] = None
        timestamp: Any = None
        media_urls: List[str] = field(default_factory=list)

    @dataclass
    class SendResult:
        success: bool = False
        message_id: Optional[str] = None
        error: Optional[str] = None

    class BasePlatformAdapter:
        def __init__(self, config: Any = None, platform: Any = None) -> None:
            self.config = config
            self.platform = platform
            self._running = False
            self._message_handler = None
            self.has_fatal_error = False
            self.fatal_error_code = None
            self.fatal_error_message = None
            self.fatal_error_retryable = True
            self.handled_events: List[Any] = []

        @property
        def is_connected(self) -> bool:
            return self._running

        def _mark_connected(self) -> None:
            self._running = True
            self.has_fatal_error = False

        def _mark_disconnected(self) -> None:
            self._running = False

        def _set_fatal_error(self, code: str, message: str, *, retryable: bool) -> None:
            self.has_fatal_error = True
            self.fatal_error_code = code
            self.fatal_error_message = message
            self.fatal_error_retryable = retryable

        def set_message_handler(self, handler) -> None:
            self._message_handler = handler

        def build_source(self, **kwargs) -> SessionSource:
            return SessionSource(platform="meshtastic", **kwargs)

        async def handle_message(self, event) -> None:
            # Record for assertions, then delegate as the real base does.
            self.handled_events.append(event)
            if self._message_handler:
                await self._message_handler(event)

    base_mod.BasePlatformAdapter = BasePlatformAdapter
    base_mod.MessageEvent = MessageEvent
    base_mod.MessageType = MessageType
    base_mod.SendResult = SendResult

    sys.modules.setdefault("gateway", gateway)
    sys.modules.setdefault("gateway.config", config_mod)
    sys.modules.setdefault("gateway.session", session_mod)
    sys.modules.setdefault("gateway.platforms", platforms_mod)
    sys.modules.setdefault("gateway.platforms.base", base_mod)
    gateway.config = config_mod  # type: ignore[attr-defined]
    gateway.platforms = platforms_mod  # type: ignore[attr-defined]
    platforms_mod.base = base_mod  # type: ignore[attr-defined]


_install_gateway_stubs()


def _ensure_platform_enum_member() -> None:
    """Make ``Platform("meshtastic")`` resolvable in tests.

    The real enum only mints a pseudo-member for a platform that is bundled
    or already registered in ``platform_registry`` — in production the
    adapter is never constructed before ``register_platform()`` has run.
    Tests construct adapters directly, so register the name up front to
    reproduce that precondition rather than working around the enum.
    """
    try:
        from gateway.platform_registry import PlatformEntry, platform_registry

        if not platform_registry.is_registered("meshtastic"):
            platform_registry.register(
                PlatformEntry(
                    name="meshtastic",
                    label="Meshtastic",
                    adapter_factory=lambda cfg: None,
                    check_fn=lambda: True,
                )
            )
    except Exception:
        pass

    try:
        from gateway.config import Platform

        Platform("meshtastic")
    except Exception:
        pass


_ensure_platform_enum_member()


@pytest.fixture(autouse=True)
def _clean_pubsub():
    """Keep global pubsub state from leaking between tests.

    ``pub.subscribe`` is process-global; a subscription surviving a test
    would make the next test's adapter receive the previous one's traffic —
    exactly the cross-talk the adapter guards against at runtime.
    """
    yield
    try:
        from pubsub import pub

        pub.unsubAll()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Ensure a stray MESHTASTIC_* var in the dev shell cannot skew a test."""
    for name in (
        "MESHTASTIC_TRANSPORT",
        "MESHTASTIC_SERIAL_PORT",
        "MESHTASTIC_TCP_HOST",
        "MESHTASTIC_NODE_NAME",
        "MESHTASTIC_ALLOWED_USERS",
        "MESHTASTIC_ALLOW_ALL_USERS",
        "MESHTASTIC_HOME_CHANNEL",
        "MESHTASTIC_EXPOSE_POSITION",
        # Loop prevention. These change whether a reply happens at all, so a
        # stray value in the dev shell would make unrelated tests silently
        # stop asserting anything.
        "MESHTASTIC_CONVERSATION_COOLDOWN_SECONDS",
        "MESHTASTIC_COOLDOWN_EXEMPT_MENTIONS",
        "MESHTASTIC_LOOP_DETECTION",
        "MESHTASTIC_LOOP_SIGNATURE_TTL_SECONDS",
        "MESHTASTIC_LOOP_SIGNATURE_MAX_ENTRIES",
        "MESHTASTIC_RATE_LIMIT_MAX_SENDS",
        "MESHTASTIC_RATE_LIMIT_WINDOW_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _clean_send_state():
    """Reset the process-global send gates between tests.

    The rate-limit window, the per-channel conversation cooldown and the
    loop-signature cache all live at module scope in ``sendpolicy`` — they
    have to, because every send path shares one gate. That makes them leak
    across tests: a test that sends five times would otherwise exhaust the
    bucket for the next one, and a test that replies on ``LongFast`` would
    leave that channel in cooldown so the following test's reply is
    suppressed and its assertion quietly passes on an empty list.
    """
    import sendpolicy as sp

    sp.reset_rate_limit()
    sp.reset_loop_state()
    yield
    sp.reset_rate_limit()
    sp.reset_loop_state()

"""The single source of truth for which Meshtastic settings are mandatory.

Hermes' generic installer reads ``requires_env`` from ``plugin.yaml`` and
prompts for whatever it finds there.  That schema has no way to say "this
variable is required *only when* another one has a particular value", so a
flat install-time prompt list can only under-ask (a tcp install with no
host) or over-ask (a serial operator made to supply a TCP host).

``plugin.yaml`` therefore declares no ``requires_env`` at all: the install
is silent, and ``hermes gateway setup`` — the one configuration path we can
verify Hermes actually invokes, via ``setup_fn`` — gathers everything.  The
conditional rules live here, in the plugin, and are enforced on the paths
this repo owns:

- :func:`validate_env` runs from the setup wizard, so the interactive path
  cannot save an incoherent configuration;
- it also runs from ``adapter.validate_config`` and
  ``MeshtasticAdapter.connect``, so a configuration assembled by hand — or
  a gateway started before the wizard was ever run — fails loudly and
  early with a message naming the exact variable and both ways to set it.

Rules are data, not code paths: add an entry to :data:`ENV_RULES` and every
consumer picks it up.

This module imports nothing outside the standard library so it stays
importable in the gateway runtime, in the CLI, and in tests.
"""

from __future__ import annotations

import os
from typing import Callable, Dict, List, Mapping, NamedTuple, Optional, Sequence

__all__ = [
    "DEFAULT_TCP_PORT",
    "SUPPORTED_TRANSPORTS",
    "UNSUPPORTED_TRANSPORTS",
    "EnvRule",
    "ENV_RULES",
    "EnvProblem",
    "check_env",
    "validate_env",
    "describe_requirements",
    "env_mapping",
    "resolve_tcp_port",
]

# The meshtastic library's own default TCP API port.  Wired through as a
# real setting because the value is not always 4403 — a radio behind a
# reverse proxy or an SSH tunnel lands on a different port.
DEFAULT_TCP_PORT = 4403

SUPPORTED_TRANSPORTS = ("serial", "tcp")
# Present in the protocol but deliberately out of scope for v1 (ROADMAP.md).
# Named separately so the error message can say "not yet" rather than
# "unknown", which is the difference between a user waiting for a release
# and a user hunting a typo.
UNSUPPORTED_TRANSPORTS = ("ble", "mqtt")

_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off")


def _get(env: Mapping[str, str], name: str) -> str:
    """Read *name* from *env*, treating whitespace-only as absent.

    An env file written by hand very often contains ``MESHTASTIC_TCP_HOST=``
    with nothing after it.  Treating that as "set" would defeat the whole
    point of these checks.
    """
    return (env.get(name) or "").strip()


def _transport(env: Mapping[str, str]) -> str:
    return _get(env, "MESHTASTIC_TRANSPORT").lower()


class EnvRule(NamedTuple):
    """One requirement on one variable.

    ``required_when`` receives the whole environment mapping, so a rule can
    depend on any other variable.  Returning ``False`` makes the variable
    optional in that configuration — it is never an error for an optional
    variable to be unset.

    ``validate`` runs only when a value is present, and returns an error
    string (or ``None`` when the value is acceptable).  Keeping the two
    concerns separate means a variable can be optional yet still rejected
    when it is set to nonsense.
    """

    name: str
    #: Why this variable exists, in the operator's terms.
    summary: str
    #: When this returns True and the variable is unset, the configuration
    #: is incomplete and is reported as a problem.
    required_when: Callable[[Mapping[str, str]], bool] = lambda env: False
    #: Human-readable statement of the condition, used in error text.
    condition: str = "always"
    #: Value check, applied whenever a value is present.
    validate: Optional[Callable[[str], Optional[str]]] = None
    #: What to do about it, shown verbatim to the operator.
    remedy: str = ""
    #: Which checks enforce this rule.  ``"install"`` rules are reported by
    #: the full configuration check but do not stop a running gateway;
    #: ``"runtime"`` rules also stop ``connect()``.  The distinction matters
    #: on upgrade: an operator who
    #: expressed their access policy in ``config.yaml`` (dm_policy /
    #: allow_from) has already made a deliberate choice, and must not be
    #: locked out of a working deployment by a newly-tightened requirement.
    #: Anything that makes the radio physically unreachable is "runtime".
    scope: str = "runtime"


def _validate_transport(value: str) -> Optional[str]:
    kind = value.strip().lower()
    if kind in SUPPORTED_TRANSPORTS:
        return None
    if kind in UNSUPPORTED_TRANSPORTS:
        return (
            f"transport '{kind}' is not supported in v1 — see ROADMAP.md. "
            f"Use 'serial' or 'tcp'."
        )
    return f"unknown transport '{value}' (expected 'serial' or 'tcp')"


def _validate_bool(value: str) -> Optional[str]:
    if value.strip().lower() in _TRUTHY + _FALSY:
        return None
    return f"expected a boolean (true/false), got {value!r}"


def _validate_tcp_port(value: str) -> Optional[str]:
    try:
        port = int(value.strip())
    except (TypeError, ValueError):
        return f"expected a port number, got {value!r}"
    if not (1 <= port <= 65535):
        return f"port {port} is out of range [1, 65535]"
    return None


#: Every ``MESHTASTIC_*`` variable the plugin reads, and whether it is
#: mandatory.  Ordered so the transport — which every other rule keys off
#: — is reported first when several are wrong at once.
ENV_RULES: Sequence[EnvRule] = (
    EnvRule(
        name="MESHTASTIC_TRANSPORT",
        summary="How to reach the radio: 'serial' (USB) or 'tcp' (WiFi).",
        required_when=lambda env: True,
        condition="always",
        validate=_validate_transport,
        remedy=(
            "Set MESHTASTIC_TRANSPORT=serial or MESHTASTIC_TRANSPORT=tcp in "
            "~/.hermes/.env, or reconfigure with: hermes gateway setup"
        ),
    ),
    EnvRule(
        name="MESHTASTIC_TCP_HOST",
        summary="Hostname or IP of the radio's TCP/WiFi API.",
        # Without this the interface cannot even be constructed: the
        # transport layer raises before a single packet is sent.
        required_when=lambda env: _transport(env) == "tcp",
        condition="when MESHTASTIC_TRANSPORT=tcp",
        remedy=(
            "Set MESHTASTIC_TCP_HOST=meshtastic.local (or the radio's IP) in "
            "~/.hermes/.env, or reconfigure with: hermes gateway setup"
        ),
    ),
    EnvRule(
        name="MESHTASTIC_ALLOW_ALL_USERS",
        summary=(
            "Whether any node in radio range may command the bot. A radio "
            "channel is public and node IDs are not authenticated, so this "
            "has to be a deliberate choice."
        ),
        # Leaving this unset is not a safe default in either direction: read
        # as true it opens the bot to every node in range, read as false it
        # leaves an operator with no allowlist unable to talk to their own
        # bot and no clue why.  Requiring the answer costs one prompt.
        required_when=lambda env: True,
        condition="always — access control must be an explicit decision",
        scope="install",
        validate=_validate_bool,
        remedy=(
            "In ~/.hermes/.env, set MESHTASTIC_ALLOW_ALL_USERS=false and list "
            "your node IDs in MESHTASTIC_ALLOWED_USERS (an empty list is fine "
            "— you can pair a node later), or set "
            "MESHTASTIC_ALLOW_ALL_USERS=true to let any node in radio range "
            "command the bot (dev only). "
            "Or reconfigure with: hermes gateway setup"
        ),
    ),
    EnvRule(
        name="MESHTASTIC_SERIAL_PORT",
        summary=(
            "Serial device path. Optional: blank autodetects a single "
            "attached radio, which is the common single-device case."
        ),
        condition="never — optional",
    ),
    EnvRule(
        name="MESHTASTIC_TCP_PORT",
        summary=(
            f"TCP API port. Optional: defaults to {DEFAULT_TCP_PORT}, which "
            "is what a stock radio listens on."
        ),
        condition="never — optional",
        validate=_validate_tcp_port,
    ),
    EnvRule(
        name="MESHTASTIC_ALLOWED_USERS",
        summary=(
            "Comma-separated node IDs allowed to command the bot. Optional: "
            "an empty allowlist is a valid deliberate state — pair a node later."
        ),
        condition="never — optional",
    ),
    EnvRule(
        name="MESHTASTIC_NODE_NAME",
        summary=(
            "Mention trigger on channels. Optional: defaults to the device's "
            "own longName, read from the radio."
        ),
        condition="never — optional",
    ),
    EnvRule(
        name="MESHTASTIC_HOME_CHANNEL",
        summary=(
            "Delivery target for cron jobs and notifications. Optional: "
            "without it only those features are unavailable."
        ),
        condition="never — optional",
    ),
    EnvRule(
        name="MESHTASTIC_EXPOSE_POSITION",
        summary="Include GPS positions in tool output. Optional: defaults to true.",
        condition="never — optional",
        validate=_validate_bool,
    ),
    EnvRule(
        name="MESHTASTIC_AUTO_INSTALL",
        summary=(
            "Install the meshtastic package on connect if missing. Optional: "
            "defaults to false."
        ),
        condition="never — optional",
        validate=_validate_bool,
    ),
)

#: Names of the rules that can ever block, for callers that want the set
#: without walking the rule objects.
CONDITIONAL_REQUIRED: Dict[str, Callable[[Mapping[str, str]], bool]] = {
    rule.name: rule.required_when for rule in ENV_RULES
}


class EnvProblem(NamedTuple):
    """One thing wrong with the environment."""

    name: str
    #: ``"missing"`` (required but unset) or ``"invalid"`` (set to nonsense).
    kind: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


def env_mapping(env: Optional[Mapping[str, str]] = None) -> Mapping[str, str]:
    """Default to the process environment.

    Hermes loads ``~/.hermes/.env`` into ``os.environ`` before the plugin
    runs, so values configured in the env file arrive here already — which
    is exactly why the checks never prompt for something already set.
    """
    return os.environ if env is None else env


def check_env(
    env: Optional[Mapping[str, str]] = None,
    *,
    scope: str = "install",
) -> List[EnvProblem]:
    """Return every problem with *env*, in rule order.  Never raises.

    An empty list means the configuration is good enough for *scope*.
    Variables that are optional and unset are never reported — that is the
    whole point: only genuine blockers block.

    *scope* is ``"install"`` (every rule) or ``"runtime"`` (only the rules
    that make the radio unreachable).  A value that is *present but
    invalid* is always reported, in either scope: a typo'd transport or an
    out-of-range port is broken however you got there.
    """
    source = env_mapping(env)
    problems: List[EnvProblem] = []
    runtime_only = scope == "runtime"

    for rule in ENV_RULES:
        value = _get(source, rule.name)

        if not value:
            if runtime_only and rule.scope != "runtime":
                continue
            if rule.required_when(source):
                problems.append(
                    EnvProblem(
                        name=rule.name,
                        kind="missing",
                        message=(
                            f"{rule.name} is required ({rule.condition}) but is not set. "
                            f"{rule.summary} {rule.remedy}".strip()
                        ),
                    )
                )
            continue

        if rule.validate is not None:
            error = rule.validate(value)
            if error:
                problems.append(
                    EnvProblem(
                        name=rule.name,
                        kind="invalid",
                        message=f"{rule.name}: {error}".strip(),
                    )
                )

    return problems


def validate_env(
    env: Optional[Mapping[str, str]] = None,
    *,
    scope: str = "install",
) -> Optional[str]:
    """One formatted message describing every problem, or ``None`` when clean.

    Returning a string rather than raising keeps the caller in control: the
    wizard prints it and re-prompts, the adapter turns it into a fatal
    error, and ``validate_config`` logs it.
    """
    problems = check_env(env, scope=scope)
    if not problems:
        return None

    header = (
        "Meshtastic is not configured correctly — "
        f"{len(problems)} problem{'s' if len(problems) != 1 else ''} found:"
    )
    lines = [f"  - {problem.message}" for problem in problems]
    return "\n".join([header, *lines])


def describe_requirements() -> List[str]:
    """Human-readable one-liners for the rules that can ever be mandatory."""
    return [
        f"{rule.name} — required {rule.condition}. {rule.summary}"
        for rule in ENV_RULES
        # A rule that is never required under any environment cannot block.
        if rule.required_when({"MESHTASTIC_TRANSPORT": "tcp"})
        or rule.required_when({"MESHTASTIC_TRANSPORT": "serial"})
    ]


def resolve_tcp_port(
    env: Optional[Mapping[str, str]] = None,
    extra: Optional[Mapping[str, object]] = None,
) -> int:
    """The TCP API port to connect to, honouring env then config then default.

    Env beats ``config.yaml`` here, matching every other setting in this
    plugin.  A malformed value falls back to the default rather than
    raising: :func:`check_env` already reports it, and refusing to connect
    twice over the same mistake helps nobody.
    """
    raw = _get(env_mapping(env), "MESHTASTIC_TCP_PORT")
    if not raw and extra:
        candidate = extra.get("tcp_port")
        raw = "" if candidate is None else str(candidate).strip()
    if not raw:
        return DEFAULT_TCP_PORT
    try:
        port = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TCP_PORT
    return port if 1 <= port <= 65535 else DEFAULT_TCP_PORT

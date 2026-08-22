"""Fill in the network settings Hermes' generic installer never asks for.

The gap this closes, reported from a real install: Hermes prompts for the
entries in ``plugin.yaml``'s ``requires_env`` and does **not** prompt for
``optional_env``.  So ``MESHTASTIC_TRANSPORT`` and
``MESHTASTIC_ALLOW_ALL_USERS`` were asked about, and the operator answered
``tcp`` — but ``MESHTASTIC_TCP_HOST`` and ``MESHTASTIC_TCP_PORT`` sit in
``optional_env`` and were never mentioned.  The install finished, and the
radio was unreachable.

Promoting ``MESHTASTIC_TCP_HOST`` into ``requires_env`` is not the fix.
That list is flat and unconditional, so every serial/USB operator would be
made to supply a TCP host they do not have and cannot meaningfully skip —
one broken install traded for another.  The conditionality has to come from
the plugin, which is the only party that knows the answer to the transport
question determines whether a host is needed at all.

So the questions are asked from here, driven by the same
:data:`envcheck.ENV_RULES` the wizard and the adapter already obey:

- a rule that is ``required_when`` the current answers is asked, and a
  blank answer is refused — the prompt repeats;
- a rule that is ``prompt_when`` the current answers is offered with its
  ``default`` pre-filled, and enter accepts it;
- a variable already set — in ``~/.hermes/.env``, in the environment, or
  answered a moment ago — is never asked about again;
- a rule with no prompt text is never asked about here at all.

Nothing is asked for a configuration that does not need it: answer
``serial`` and neither TCP question appears.

CLI helpers are imported lazily, exactly as ``setup_wizard`` does, so this
module stays importable in the gateway runtime and in tests with no
``hermes_cli`` present.
"""

from __future__ import annotations

import sys
from typing import Callable, Dict, List, Mapping, Optional

try:
    from .envcheck import ENV_RULES, check_env, env_mapping
except ImportError:  # pragma: no cover - direct-import context
    from envcheck import ENV_RULES, check_env, env_mapping  # type: ignore[no-redef]

__all__ = [
    "MissingConfiguration",
    "pending_prompts",
    "prompt_for_missing",
    "ensure_configured",
]

#: Shown in the "add this to ~/.hermes/.env" hint for a variable that has no
#: default of its own.  A concrete, plausible value beats ``<value>``.
_EXAMPLES = {
    "MESHTASTIC_TCP_HOST": "meshtastic.local",
    "MESHTASTIC_TRANSPORT": "serial",
    "MESHTASTIC_ALLOW_ALL_USERS": "false",
}


class MissingConfiguration(RuntimeError):
    """A required value is absent and there is no one to ask.

    Raised instead of blocking on ``input()`` when stdin is not a terminal
    — a piped install, CI, or ``--yes``.  The message names the variable
    and the exact line to add, so the operator has something to act on
    rather than a hung process or a config that silently cannot connect.
    """


def _is_interactive() -> bool:
    """Whether there is a human on the other end of stdin.

    Checked before every prompt rather than once at import: a caller may
    have redirected streams in between, and hanging on ``input()`` in a
    non-interactive install is the failure mode this module exists to
    avoid.
    """
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except Exception:
        # A closed or exotic stream is not a human.
        return False


def _env_line(name: str, rule) -> str:
    """The literal ``.env`` line that would satisfy *rule*."""
    return f"{name}={rule.default or _EXAMPLES.get(name, '<value>')}"


def pending_prompts(env: Optional[Mapping[str, str]] = None) -> List:
    """Rules that still need an answer, in ``ENV_RULES`` order.

    Both kinds of question are included: the mandatory ones (required under
    this configuration and unset) and the offered ones (optional, but worth
    a pre-filled prompt because the default is not always right).  A rule
    with no ``prompt`` text is skipped — the wizard covers those in richer
    steps, and duplicating them here would ask the same question twice.
    """
    source = env_mapping(env)
    pending = []
    for rule in ENV_RULES:
        if not rule.prompt:
            continue
        if (source.get(rule.name) or "").strip():
            continue  # already configured — do not re-ask
        if rule.required_when(source) or rule.prompt_when(source):
            pending.append(rule)
    return pending


def _ask(rule, prompt_fn: Callable[..., str], notify: Callable[[str], None]) -> str:
    """Ask for one value, re-prompting until it is acceptable.

    Validation happens here rather than after the whole run, so a typo'd
    port is corrected while the operator is still looking at the question
    instead of being reported at the end of an install they thought had
    succeeded.
    """
    mandatory = not rule.default
    while True:
        raw = prompt_fn(rule.prompt, default=rule.default)
        value = (raw or "").strip()

        if not value:
            if rule.default:
                # Enter accepts the pre-filled default.
                value = rule.default
            elif mandatory:
                # A blank or whitespace-only answer to a mandatory question
                # is not an answer.  Say why, and ask again.
                notify(f"{rule.name} is required {rule.condition}. {rule.summary}")
                continue

        if rule.validate is not None:
            error = rule.validate(value)
            if error:
                notify(f"{rule.name}: {error}")
                continue

        return value


def prompt_for_missing(
    env: Optional[Mapping[str, str]] = None,
    *,
    prompt_fn: Optional[Callable[..., str]] = None,
    save_fn: Optional[Callable[[str, str], None]] = None,
    notify: Optional[Callable[[str], None]] = None,
) -> Dict[str, str]:
    """Ask for every pending value and persist it.  Returns what was set.

    *prompt_fn* and *save_fn* default to ``hermes_cli.setup``'s helpers,
    imported lazily so this module never hard-depends on the CLI.  Tests —
    and any caller with its own UI — pass their own.

    The working mapping is updated after each answer, so a later rule sees
    an earlier one: answering ``tcp`` to the transport question is exactly
    what makes the host question appear.
    """
    if prompt_fn is None or save_fn is None:
        from hermes_cli.setup import prompt as cli_prompt, save_env_value

        prompt_fn = prompt_fn or cli_prompt
        save_fn = save_fn or save_env_value

    say = notify or (lambda message: print(f"  {message}"))

    working: Dict[str, str] = dict(env_mapping(env))
    answered: Dict[str, str] = {}

    # Recomputed each pass: an answer can make a new rule pending (choose
    # tcp and the host becomes required) or retire one.  ``answered``
    # guards against re-asking a question that was answered with the empty
    # string, which would otherwise still read as unset.
    while True:
        pending = [r for r in pending_prompts(working) if r.name not in answered]
        if not pending:
            break
        rule = pending[0]

        if not _is_interactive():
            if rule.required_when(working):
                raise MissingConfiguration(
                    f"{rule.name} is required ({rule.condition}) and cannot be "
                    f"prompted for: stdin is not a terminal.\n"
                    f"Add this line to ~/.hermes/.env:\n"
                    f"    {_env_line(rule.name, rule)}\n"
                    f"then re-run the install, or run: hermes gateway setup"
                )
            # Optional and unaskable: its default is correct by
            # construction, so leave it unset rather than guessing.
            answered[rule.name] = ""
            working[rule.name] = ""
            continue

        value = _ask(rule, prompt_fn, say)
        save_fn(rule.name, value)
        answered[rule.name] = value
        working[rule.name] = value

    return {name: value for name, value in answered.items() if value}


def ensure_configured(
    env: Optional[Mapping[str, str]] = None,
    *,
    prompt_fn: Optional[Callable[..., str]] = None,
    save_fn: Optional[Callable[[str, str], None]] = None,
    notify: Optional[Callable[[str], None]] = None,
) -> Dict[str, str]:
    """Prompt for what is missing, then confirm nothing required is left.

    The confirmation is not redundant: a rule with no ``prompt`` text is
    never asked about here, and a non-interactive run deliberately skips
    the optional ones.  Raising rather than returning ``False`` keeps the
    reason attached to the failure.
    """
    answered = prompt_for_missing(
        env, prompt_fn=prompt_fn, save_fn=save_fn, notify=notify
    )

    combined = dict(env_mapping(env))
    combined.update(answered)
    problems = [p for p in check_env(combined, scope="install") if p.kind == "missing"]
    if not problems:
        return answered

    rules = {rule.name: rule for rule in ENV_RULES}
    lines = [f"    {_env_line(p.name, rules[p.name])}" for p in problems if p.name in rules]
    raise MissingConfiguration(
        "Meshtastic is not configured — "
        + "; ".join(p.message for p in problems)
        + "\nAdd to ~/.hermes/.env:\n"
        + "\n".join(lines)
    )

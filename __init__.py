"""MeshHermes — a Meshtastic LoRa mesh platform plugin for Hermes Agent.

The Hermes plugin loader imports this file as a package rooted at the
plugin directory and calls :func:`register`.

The import below is deliberately tolerant.  Under Hermes this module has a
parent package and the relative import resolves normally.  Under pytest the
repo root is on ``sys.path``, so this file can also be imported *without* a
package context; rather than raising ``ImportError`` and breaking test
collection, we fall back to a top-level import.
"""

try:  # normal case: loaded by Hermes as a package
    from .adapter import (
        check_env_ready,
        complete_install_config,
        register,
    )
    from .setup_wizard import interactive_setup as setup
except ImportError:  # pragma: no cover - test/collection context
    try:
        from adapter import (  # type: ignore[no-redef]
            check_env_ready,
            complete_install_config,
            register,
        )
        from setup_wizard import interactive_setup as setup  # type: ignore[no-redef]
    except ImportError:
        register = None  # type: ignore[assignment]
        check_env_ready = None  # type: ignore[assignment]
        complete_install_config = None  # type: ignore[assignment]
        setup = None  # type: ignore[assignment]

#: Exported at package level, not just on the adapter, so an installer that
#: looks for a post-install hook on the plugin module finds one under any of
#: the names such hooks conventionally use.  All three are the same flow:
#: ask for the conditionally-required network settings (a TCP host is
#: mandatory once the transport is tcp; the port is offered pre-filled with
#: 4403) that ``plugin.yaml``'s flat ``requires_env`` cannot express.
post_install = complete_install_config
configure = complete_install_config

__all__ = [
    "register",
    "check_env_ready",
    "complete_install_config",
    "post_install",
    "configure",
    "setup",
]
__version__ = "1.0.0"

"""MeshHermes — a Meshtastic LoRa mesh platform plugin for Hermes Agent.

The Hermes plugin loader imports this file as a package rooted at the
plugin directory and calls :func:`register`.

Installing this plugin asks nothing.  Configuration happens afterwards, in
``hermes gateway setup`` — see :data:`POST_INSTALL_MESSAGE`.

The import below is deliberately tolerant.  Under Hermes this module has a
parent package and the relative import resolves normally.  Under pytest the
repo root is on ``sys.path``, so this file can also be imported *without* a
package context; rather than raising ``ImportError`` and breaking test
collection, we fall back to a top-level import.
"""

try:  # normal case: loaded by Hermes as a package
    from .adapter import (
        POST_INSTALL_MESSAGE,
        check_env_ready,
        post_install_message,
        register,
    )
except ImportError:  # pragma: no cover - test/collection context
    try:
        from adapter import (  # type: ignore[no-redef]
            POST_INSTALL_MESSAGE,
            check_env_ready,
            post_install_message,
            register,
        )
    except ImportError:
        register = None  # type: ignore[assignment]
        check_env_ready = None  # type: ignore[assignment]
        post_install_message = None  # type: ignore[assignment]
        POST_INSTALL_MESSAGE = ""  # type: ignore[assignment]

__all__ = [
    "register",
    "check_env_ready",
    "post_install_message",
    "POST_INSTALL_MESSAGE",
]
__version__ = "1.0.7"

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
    from .adapter import register
except ImportError:  # pragma: no cover - test/collection context
    try:
        from adapter import register  # type: ignore[no-redef]
    except ImportError:
        register = None  # type: ignore[assignment]

__all__ = ["register"]
__version__ = "1.0.6"

"""Node and telemetry cache backing the mesh_* tools.

A thin, bounded cache over the library's ``iface.nodes`` dict, kept fresh
from ``meshtastic.node.updated`` and ``meshtastic.receive.telemetry``.

Two constraints shape this module:

**Threading.**  Writes arrive on the library's receive thread; reads happen
in tool handlers on the event loop.  Every access takes a lock and returns
copies, so a caller can never observe a half-updated record.

**Privacy.**  Node positions are the GPS coordinates of real people.  They
are never logged, are rounded to a configurable precision in tool output,
and can be suppressed entirely with ``MESHTASTIC_EXPOSE_POSITION=false``.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

try:
    from .normalize import node_num_to_hex
except ImportError:  # pragma: no cover - direct-import context
    from normalize import node_num_to_hex  # type: ignore[no-redef]

__all__ = ["NodeDB", "DEFAULT_POSITION_PRECISION", "MAX_TELEMETRY_SAMPLES"]

# 4 decimal places ≈ 11 m — enough to be useful, coarse enough not to
# pinpoint someone's front door.
DEFAULT_POSITION_PRECISION = 4

# Bounded history so "trend" questions can be answered without unbounded
# memory growth on a busy mesh.
MAX_TELEMETRY_SAMPLES = 100


def _node_id_of(record: Dict[str, Any]) -> Optional[str]:
    """Best-effort canonical ``!hex`` id for a node record."""
    user = record.get("user") or {}
    raw = user.get("id")
    if isinstance(raw, str) and raw.startswith("!"):
        return raw.lower()
    num = record.get("num")
    if isinstance(num, int):
        try:
            return node_num_to_hex(num)
        except ValueError:
            return None
    return None


class NodeDB:
    """Thread-safe cache of mesh node state and telemetry history."""

    def __init__(self, max_samples: int = MAX_TELEMETRY_SAMPLES) -> None:
        self._lock = threading.RLock()
        self._nodes: Dict[str, Dict[str, Any]] = {}
        self._telemetry: Dict[str, Deque[Dict[str, Any]]] = {}
        self._max_samples = max_samples

    # ── Writes (receive thread) ───────────────────────────────────────────

    def snapshot_from_interface(self, iface: Any) -> int:
        """Seed the cache from ``iface.nodes``.  Returns the node count.

        Called once the connection is established, so the tools have data
        without waiting for organic node updates to trickle in.
        """
        nodes = getattr(iface, "nodes", None) or {}
        count = 0
        with self._lock:
            for record in nodes.values():
                if not isinstance(record, dict):
                    continue
                node_id = _node_id_of(record)
                if node_id:
                    self._nodes[node_id] = dict(record)
                    count += 1
        return count

    def update_node(self, record: Dict[str, Any]) -> None:
        """Merge a single node record (from ``meshtastic.node.updated``)."""
        if not isinstance(record, dict):
            return
        node_id = _node_id_of(record)
        if not node_id:
            return
        with self._lock:
            existing = self._nodes.get(node_id, {})
            merged = dict(existing)
            merged.update(record)
            self._nodes[node_id] = merged

    def record_telemetry(self, node_id: str, metrics: Dict[str, Any]) -> None:
        """Append a telemetry sample for *node_id*, bounded by max_samples."""
        if not node_id or not isinstance(metrics, dict):
            return
        sample = dict(metrics)
        sample.setdefault("timestamp", time.time())
        with self._lock:
            history = self._telemetry.get(node_id)
            if history is None:
                history = deque(maxlen=self._max_samples)
                self._telemetry[node_id] = history
            history.append(sample)

    # ── Reads (tool handlers) ─────────────────────────────────────────────

    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            record = self._nodes.get((node_id or "").lower())
            return dict(record) if record else None

    def all_nodes(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._nodes.values()]

    def telemetry_history(self, node_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            history = self._telemetry.get((node_id or "").lower())
            if not history:
                return []
            samples = list(history)
        return [dict(s) for s in samples[-limit:]]

    def node_count(self) -> int:
        with self._lock:
            return len(self._nodes)

    def clear(self) -> None:
        with self._lock:
            self._nodes.clear()
            self._telemetry.clear()

    # ── Presentation ──────────────────────────────────────────────────────

    def describe_node(
        self,
        record: Dict[str, Any],
        expose_position: bool = True,
        precision: int = DEFAULT_POSITION_PRECISION,
    ) -> Dict[str, Any]:
        """Flatten a raw node record into the shape the tools return.

        Position is included only when *expose_position* is set, and is
        always rounded to *precision* decimal places.
        """
        user = record.get("user") or {}
        metrics = record.get("deviceMetrics") or {}
        out: Dict[str, Any] = {
            "id": _node_id_of(record),
            "long_name": user.get("longName"),
            "short_name": user.get("shortName"),
            "hops_away": record.get("hopsAway"),
            "snr": record.get("snr"),
            "rssi": record.get("rssi"),
            "last_heard": record.get("lastHeard"),
        }
        if metrics:
            out["battery_level"] = metrics.get("batteryLevel")
            out["voltage"] = metrics.get("voltage")
            out["channel_utilization"] = metrics.get("channelUtilization")
            out["air_util_tx"] = metrics.get("airUtilTx")

        position = record.get("position") or {}
        if expose_position and position:
            lat = position.get("latitude")
            lon = position.get("longitude")
            if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                out["position"] = {
                    "latitude": round(float(lat), precision),
                    "longitude": round(float(lon), precision),
                    "altitude": position.get("altitude"),
                    "precision_note": f"rounded to {precision} decimal places",
                }
        elif position and not expose_position:
            out["position"] = "suppressed (MESHTASTIC_EXPOSE_POSITION=false)"

        return {k: v for k, v in out.items() if v is not None}

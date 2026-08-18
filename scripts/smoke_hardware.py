#!/usr/bin/env python3
"""Manual smoke test against a real Meshtastic radio.

**Never runs in CI** — it needs hardware and it transmits.

    python scripts/smoke_hardware.py --transport serial --port /dev/ttyUSB0
    python scripts/smoke_hardware.py --transport tcp --host meshtastic.local

Each step prints PASS or FAIL.  Transmitting requires a correctly-set LoRa
region and compliance with your local RF regulations; the script refuses to
transmit when the region is UNSET rather than setting it for you.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import transport as tp  # noqa: E402
from chunking import MESHTASTIC_HARD_LIMIT, chunk_text  # noqa: E402
from normalize import node_num_to_hex  # noqa: E402

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
INFO = "\033[36m····\033[0m"

_failures = 0


def report(ok: bool, label: str, detail: str = "") -> bool:
    global _failures
    if not ok:
        _failures += 1
    print(f"  [{PASS if ok else FAIL}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def info(message: str) -> None:
    print(f"  [{INFO}] {message}")


async def run(args: argparse.Namespace) -> int:
    print("\nMeshHermes hardware smoke test")
    print("=" * 60)

    # ── 1. Connect ────────────────────────────────────────────────────────
    print("\n1. Connect and read device info")
    try:
        iface = await tp.open_interface(
            transport=args.transport, serial_port=args.port, tcp_host=args.host
        )
    except Exception as e:
        report(False, "open interface", str(e))
        return 1
    report(True, "interface opened")

    try:
        my_num = tp.get_my_node_num(iface)
        report(my_num is not None, "read own node number",
               node_num_to_hex(my_num) if my_num is not None else "unavailable")

        long_name = tp.get_my_long_name(iface)
        report(bool(long_name), "read device long name", str(long_name))
        info("this plugin never renames the device — use the meshtastic CLI")

        # ── 2. Region ─────────────────────────────────────────────────────
        print("\n2. Check LoRa region")
        region = tp.read_region(iface)
        info(f"region reported as: {region}")
        if tp.region_is_unset(region):
            report(False, "region configured",
                   "UNSET — run: meshtastic --set lora.region <REGION>")
            print("\nRefusing to transmit with an unset region. Aborting.")
            return 1
        report(True, "region configured", str(region))

        # ── 3. Nodes ──────────────────────────────────────────────────────
        print("\n3. List known nodes")
        nodes = getattr(iface, "nodes", None) or {}
        report(len(nodes) > 0, "node database populated", f"{len(nodes)} node(s)")
        for node_id, record in list(nodes.items())[:10]:
            user = (record or {}).get("user") or {}
            info(f"{node_id}  {user.get('longName', '?')}  snr={record.get('snr')}")

        # ── 4. Chunking over the air ──────────────────────────────────────
        print("\n4. Send a >200-byte message to self (verifies chunking)")
        if args.skip_tx:
            info("skipped (--skip-tx)")
        else:
            long_text = (
                "MeshHermes smoke test. " + " ".join(f"word{i}" for i in range(60))
            )
            chunks = chunk_text(long_text)
            report(len(chunks) > 1, "message split into chunks", f"{len(chunks)} chunks")
            oversize = [c for c in chunks if len(c.encode("utf-8")) > MESHTASTIC_HARD_LIMIT]
            report(not oversize, "every chunk fits a LoRa frame")

            dest = node_num_to_hex(my_num) if my_num is not None else None
            for index, chunk in enumerate(chunks, 1):
                info(f"transmitting chunk {index}/{len(chunks)} ({len(chunk.encode('utf-8'))} bytes)")
                await tp.send_text(iface, chunk, dest_id=dest)
                if index < len(chunks):
                    await asyncio.sleep(1.5)  # pace the radio queue
            report(True, "all chunks transmitted")

        # ── 5. Inbound from another node ──────────────────────────────────
        print("\n5. Inbound message test")
        if args.skip_rx:
            info("skipped (--skip-rx)")
        else:
            received: list = []

            def on_text(packet=None, interface=None, **kwargs):
                if interface is not iface or not isinstance(packet, dict):
                    return
                text = (packet.get("decoded") or {}).get("text")
                if text:
                    received.append((packet.get("fromId"), text))
                    print(f"  [{INFO}] received from {packet.get('fromId')}: {text}")

            tp.subscribe(on_text, tp.TOPIC_RECEIVE_TEXT)
            my_id = node_num_to_hex(my_num) if my_num is not None else "this node"
            print(f"\n  >>> Now DM this radio ({my_id}) from another node.")
            print(f"  >>> Waiting {args.wait}s...\n")
            try:
                for _ in range(args.wait):
                    if received:
                        break
                    await asyncio.sleep(1)
            finally:
                tp.unsubscribe(on_text, tp.TOPIC_RECEIVE_TEXT)
            report(bool(received), "inbound message received",
                   "" if received else f"nothing arrived within {args.wait}s")

    finally:
        # ── 6. Clean disconnect ───────────────────────────────────────────
        print("\n6. Disconnect and release the port")
        await tp.close_interface(iface)
        report(True, "interface closed")
        if args.transport == "serial" and args.port:
            try:
                import serial

                probe = serial.Serial(args.port)
                probe.close()
                report(True, "serial port released", args.port)
            except ImportError:
                info("pyserial not available — skipping port-release probe")
            except Exception as e:
                report(False, "serial port released", str(e))

    print("\n" + "=" * 60)
    if _failures:
        print(f"{_failures} check(s) FAILED")
        return 1
    print("All checks PASSED")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=("serial", "tcp"), default="serial")
    parser.add_argument("--port", default="", help="serial device, e.g. /dev/ttyUSB0")
    parser.add_argument("--host", default="", help="TCP hostname/IP")
    parser.add_argument("--wait", type=int, default=60, help="seconds to wait for an inbound DM")
    parser.add_argument("--skip-tx", action="store_true", help="do not transmit")
    parser.add_argument("--skip-rx", action="store_true", help="skip the inbound test")
    args = parser.parse_args()

    if args.transport == "tcp" and not args.host:
        parser.error("--host is required for --transport tcp")

    print("\n⚠️  This script TRANSMITS on a physical radio.")
    print("   Ensure your LoRa region is set correctly and that you are")
    print("   complying with local RF regulations.")

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())

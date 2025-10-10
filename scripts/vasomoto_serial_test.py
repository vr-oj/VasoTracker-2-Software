#!/usr/bin/env python3
"""
Standalone hardware smoke test for the VasoMoto Arduino sketch.

The script relies on the reusable :class:`VasoMotoClient` helper to discover the
serial-connected Arduino, stream telemetry, and send bracketed setpoint
commands (``<60>`` for 60 mmHg).  Use it to confirm cabling, firmware, and the
host-side environment before integrating the behaviour into the full
VasoTracker application.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vasomoto_client import VasoMotoClient


def _format_fields(fields: Iterable[tuple[str, object]]) -> str:
    return ", ".join(f"{key}={value}" for key, value in fields)


def list_ports() -> None:
    ports = VasoMotoClient.discover_ports()
    if not ports:
        print("No serial ports found. Plug in the Arduino and try again.\n")
        return
    print("Detected serial ports:")
    for device, description in ports:
        if description:
            print(f"  {device:>16}  {description}")
        else:
            print(f"  {device:>16}")
    print()


def telemetry_loop(client: VasoMotoClient, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        telemetry = client.get_latest(timeout=0.5)
        if not telemetry:
            continue
        timestamp = time.strftime("%H:%M:%S")
        if telemetry.fields:
            print(f"[{timestamp}] {telemetry.raw_line}  ({_format_fields(telemetry.fields.items())})", flush=True)
        else:
            print(f"[{timestamp}] {telemetry.raw_line}", flush=True)
    print("Telemetry loop stopped.", flush=True)


def interactive_prompt(client: VasoMotoClient) -> None:
    print("Enter a target pressure in mmHg, 'raw <payload>' for manual commands, or 'q' to exit.")
    while True:
        try:
            user_input = input("mmHg> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue

        lowered = user_input.lower()
        if lowered in {"q", "quit", "exit"}:
            break
        if lowered.startswith("raw "):
            payload = user_input[4:].strip()
            if not payload:
                print("Nothing to send. Usage: raw <payload>")
                continue
            try:
                client.send_raw(payload)
                print(f"[cmd] sent raw payload: {payload}")
            except Exception as exc:  # pragma: no cover - hardware I/O
                print(f"[warn] failed to send raw payload: {exc}")
            continue

        try:
            target = float(user_input)
        except ValueError:
            print("Unrecognised input. Provide a numeric mmHg value or use 'raw <payload>'.")
            continue

        try:
            ok, message = client.set_pressure(target, style="<{v}>\n")
            status = "ok" if ok else "warn"
            print(f"[{status}] {message}")
        except Exception as exc:  # pragma: no cover - hardware I/O
            print(f"[warn] failed to set pressure: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Standalone VasoMoto Arduino connectivity test.")
    parser.add_argument("--port", default=None, help="Serial port path (e.g. COM3, /dev/ttyACM0). Auto-detects if omitted.")
    parser.add_argument("--baud", type=int, default=None, help="Baud rate. Auto-detects if omitted.")
    parser.add_argument("--set", type=float, default=None, help="Send an initial pressure command (mmHg).")
    parser.add_argument("--no-interactive", action="store_true", help="Only stream telemetry; skip the interactive prompt.")
    args = parser.parse_args()

    list_ports()

    client = VasoMotoClient(port=args.port, baud=args.baud)
    try:
        client.connect()
    except Exception as exc:  # pragma: no cover - hardware I/O
        print(f"Failed to connect to the Arduino: {exc}")
        return 1

    print(f"Connected on {client.port} @ {client.baud} baud.\n")

    stop_event = threading.Event()
    thread = threading.Thread(target=telemetry_loop, args=(client, stop_event), daemon=True)
    thread.start()

    if args.set is not None:
        try:
            ok, message = client.set_pressure(args.set, style="<{v}>\n")
            status = "ok" if ok else "warn"
            print(f"[{status}] Initial set command: {message}")
        except Exception as exc:  # pragma: no cover - hardware I/O
            print(f"[warn] failed to send initial pressure command: {exc}")

    try:
        if args.no_interactive:
            print("Streaming telemetry. Press Ctrl+C to exit.")
            while True:
                time.sleep(0.5)
        else:
            interactive_prompt(client)
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    finally:
        stop_event.set()
        thread.join(timeout=1.0)
        client.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())

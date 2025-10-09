# #################################################
# # VasoTracker 2 - Blood Vessel Diameter Measurement Software
# #
# # Author: Calum Wilson, Matthew D Lee, and Chris Osborne
# # License: BSD 3-Clause License (See main file for details)
# # Website: www.vasostracker.com
# #
# #################################################

"""
Tiny REST bridge that exposes the VasoMoto serial client over HTTP.

Run this service locally so any application can set pressures via
`GET /set_pressure?mmHg=<value>` and subscribe to live telemetry through a
Server-Sent Events stream at `/stream`.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Generator, Optional

try:
    from flask import Flask, Response, jsonify, request
except ImportError as exc:  # pragma: no cover - runtime dependency
    raise ImportError(
        "Flask is required for the REST bridge. Install with `pip install flask`."
    ) from exc

from vasomoto_client import VasoMotoClient

app = Flask(__name__)
client: Optional[VasoMotoClient] = None
telemetry_queue: "queue.Queue[str]" = queue.Queue(maxsize=1000)


def telemetry_worker() -> None:
    """Background thread that forwards telemetry from the client to the SSE queue."""
    while True:
        if client is None or not client.connected:
            time.sleep(0.2)
            continue
        telemetry = client.get_latest(timeout=0.5)
        if not telemetry:
            continue
        try:
            telemetry_queue.put_nowait(telemetry.raw_line)
        except queue.Full:
            try:
                telemetry_queue.get_nowait()
                telemetry_queue.put_nowait(telemetry.raw_line)
            except Exception:
                pass


@app.route("/status")
def status() -> "Response":
    if client is None or not client.connected:
        return jsonify(ok=False, msg="Client not connected"), 503
    return jsonify(
        ok=True,
        port=client.port,
        baud=client.baud,
        last_line=client.last_line,
        last_fields=client.last_fields,
    )


@app.route("/set_pressure")
def set_pressure() -> "Response":
    if client is None or not client.connected:
        return jsonify(ok=False, msg="Client not connected"), 503
    try:
        value = float(request.args.get("mmHg", ""))
    except Exception:
        return jsonify(ok=False, msg="mmHg query parameter required"), 400
    ok, message = client.set_pressure(value)
    return jsonify(ok=ok, msg=message)


@app.route("/stream")
def stream() -> "Response":
    def generator() -> Generator[str, None, None]:
        yield "retry: 500\n\n"
        while True:
            line = telemetry_queue.get()
            yield f"data: {line}\n\n"

    return Response(generator(), mimetype="text/event-stream")


def main() -> None:
    global client
    client = VasoMotoClient()
    client.connect()

    thread = threading.Thread(target=telemetry_worker, daemon=True)
    thread.start()

    app.run(host="127.0.0.1", port=8414, threaded=True)


if __name__ == "__main__":
    main()

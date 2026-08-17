#!/usr/bin/env python3
"""
Relay server for the ambilight demo/calibration tools.

- Runs a WebSocket server (default port 8765) that capture.py connects to
  as a "producer", and that browser pages connect to as "viewers".
  Anything the producer sends gets broadcast to all connected viewers.
- Also serves files over plain HTTP (default port 8080).

Usage:
    python3 server.py
    python3 server.py --ws-port 8765 --http-port 8080
"""

import argparse
import asyncio
import contextlib
import json
import subprocess
import threading
import http.server
import socketserver
from pathlib import Path
import time
from urllib.parse import urlparse

import websockets

viewers = set()
producers = set()
last_config = None  # cache so late-joining viewers get the current zone layout
frames_routed = 0
last_frame_time = None
started_at = time.time()
CONFIG_PATH = Path(__file__).with_name("config.json")
SERVICE_NAME = "ambilight-led.service"


async def handler(websocket):
    global last_config, frames_routed, last_frame_time

    # First message from any client declares its role.
    try:
        first = await asyncio.wait_for(websocket.recv(), timeout=10)
        data = json.loads(first)
    except Exception as exc:
        print(f"Client disconnected before role registration: {exc}")
        return

    role = data.get("role", "viewer")

    if role == "producer":
        producers.add(websocket)
        print("Producer connected.")
        try:
            async for message in websocket:
                is_binary_frame = isinstance(message, (bytes, bytearray))
                msg_type = "frame" if is_binary_frame else None

                if not is_binary_frame:
                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError:
                        print("Dropping malformed JSON from producer.")
                        continue

                    msg_type = data.get("type")
                    if msg_type == "config":
                        last_config = message

                dead = set()
                for v in list(viewers):
                    try:
                        await v.send(message)
                    except websockets.ConnectionClosed:
                        dead.add(v)
                viewers.difference_update(dead)

                if msg_type == "frame":
                    frames_routed += 1
                    last_frame_time = time.time()
                    if frames_routed % 300 == 0:
                        print(f"Routed {frames_routed} frames to {len(viewers)} viewer(s).")
        finally:
            producers.discard(websocket)
            print("Producer disconnected.")
    else:
        viewers.add(websocket)
        print(f"Viewer connected. ({len(viewers)} total)")
        if last_config:
            await websocket.send(last_config)
        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    print("Dropping malformed JSON from viewer.")
                    continue

                if data.get("type") not in {"calibration", "command"}:
                    continue

                dead = set()
                for producer in list(producers):
                    try:
                        await producer.send(message)
                    except websockets.ConnectionClosed:
                        dead.add(producer)
                producers.difference_update(dead)
        except websockets.ConnectionClosed as exc:
            print(f"Viewer connection closed ({exc.code}).")
        finally:
            viewers.discard(websocket)
            print(f"Viewer disconnected. ({len(viewers)} total)")


def public_status():
    now = time.time()
    return {
        "ok": True,
        "uptime_sec": round(now - started_at, 1),
        "viewers": len(viewers),
        "producers": len(producers),
        "frames_routed": frames_routed,
        "last_frame_age_sec": None if last_frame_time is None else round(now - last_frame_time, 2),
        "has_config": last_config is not None,
        "config_path": str(CONFIG_PATH),
    }


def merge_config_update(update):
    existing = {}
    if CONFIG_PATH.exists():
        with contextlib.suppress(Exception):
            existing = json.loads(CONFIG_PATH.read_text())
    existing.update(update)
    CONFIG_PATH.write_text(json.dumps(existing, indent=2) + "\n")
    return existing


def run_control_command(action):
    commands = {
        "status": ["systemctl", "status", SERVICE_NAME, "--no-pager"],
        "restart": ["sudo", "-n", "systemctl", "restart", SERVICE_NAME],
        "stop": ["sudo", "-n", "systemctl", "stop", SERVICE_NAME],
    }
    if action not in commands:
        return {"ok": False, "error": f"unsupported action: {action}"}
    proc = subprocess.run(commands[action], text=True, capture_output=True, timeout=20)
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-4000:],
    }


def recent_logs():
    proc = subprocess.run(
        ["journalctl", "-u", SERVICE_NAME, "-n", "120", "--no-pager"],
        text=True,
        capture_output=True,
        timeout=20,
    )
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "logs": proc.stdout[-12000:],
        "stderr": proc.stderr[-4000:],
    }


def start_http_server(http_port, directory):
    class ReusableThreadingTCPServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True

    class AmbilightHttpHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)

        def send_json(self, status, payload):
            body = json.dumps(payload, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def read_json(self):
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/api/status":
                self.send_json(200, public_status())
                return
            if path == "/api/logs":
                self.send_json(200, recent_logs())
                return
            if path == "/api/config":
                if CONFIG_PATH.exists():
                    self.send_json(200, json.loads(CONFIG_PATH.read_text()))
                else:
                    self.send_json(404, {"ok": False, "error": "config.json not found"})
                return
            super().do_GET()

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                payload = self.read_json()
                if path == "/api/config":
                    saved = merge_config_update(payload)
                    self.send_json(200, {"ok": True, "config": saved})
                    return
                if path == "/api/service":
                    self.send_json(200, run_control_command(str(payload.get("action", ""))))
                    return
                self.send_json(404, {"ok": False, "error": "not found"})
            except Exception as exc:
                self.send_json(500, {"ok": False, "error": str(exc)})

    handler_cls = AmbilightHttpHandler
    with ReusableThreadingTCPServer(("0.0.0.0", http_port), handler_cls) as httpd:
        httpd.serve_forever()


async def main():
    parser = argparse.ArgumentParser(description="Ambilight demo relay + HTTP server")
    parser.add_argument("--ws-port", type=int, default=8765)
    parser.add_argument("--http-port", type=int, default=8080)
    parser.add_argument("--dir", type=str, default=".", help="Directory to serve index.html from")
    args = parser.parse_args()
    serve_dir = str(Path(args.dir).resolve())

    threading.Thread(
        target=start_http_server, args=(args.http_port, serve_dir), daemon=True
    ).start()

    print(f"Serving:    {serve_dir}")
    print(f"Demo page:  http://localhost:{args.http_port}/index.html")
    print(f"Calibrate:  http://localhost:{args.http_port}/calibrate.html")
    print(f"WebSocket:  ws://0.0.0.0:{args.ws_port}  (capture7.py connects here)")

    async with websockets.serve(
        handler,
        "0.0.0.0",
        args.ws_port,
        max_size=8 * 1024 * 1024,
        ping_interval=20,
        ping_timeout=20,
    ):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")

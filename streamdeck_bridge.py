#!/usr/bin/env python3
"""
Stream Deck HTTP bridge for Workload Tracker.

Run this alongside tracker.py. It exposes a tiny HTTP server on localhost:7373
that the Stream Deck (via the "System: Website" or a plugin like
"Touch Portal" or "deckboard") can call to trigger actions.

Usage:
    python3 streamdeck_bridge.py

Stream Deck button configs (set URL in "Open URL" action):
    http://localhost:7373/timer/toggle    — toggle timer on last-active task
    http://localhost:7373/status/next     — cycle status on selected task
    http://localhost:7373/log/15          — log 15 minutes on selected task
    http://localhost:7373/log/30          — log 30 minutes on selected task
    http://localhost:7373/log/60          — log 60 minutes on selected task
    http://localhost:7373/filter/demokit  — filter to DemoKit
    http://localhost:7373/filter/demos    — filter to Demos & Workshops
    http://localhost:7373/filter/strategic— filter to Strategic Deals
    http://localhost:7373/filter/other    — filter to Other
    http://localhost:7373/filter/all      — show all roles
    http://localhost:7373/status          — get current tracker state (JSON)

All actions write to ~/.workload_tracker.json (same file as the TUI).
"""

import json
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse
import sys

DATA_FILE = Path.home() / ".workload_tracker.json"


def uid() -> str:
    import random, string
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d%H%M%S") + "".join(random.choices(string.ascii_lowercase, k=4))


def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception:
            pass
    return {"tasks": [], "active_timer": None}


def save_data(data: dict):
    DATA_FILE.write_text(json.dumps(data, indent=2))


def task_logged_mins(task: dict) -> float:
    return sum(l.get("minutes", 0) for l in task.get("logs", []))


def fmt_mins(mins: float) -> str:
    if not mins:
        return "0m"
    h = int(mins // 60)
    m = int(mins % 60)
    return f"{h}h {m}m" if h else f"{m}m"


class BridgeHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress default logging; use custom
        print(f"[bridge] {self.path} → {args[1] if len(args) > 1 else ''}")

    def respond(self, body: dict, status: int = 200):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = urlparse(self.path).path.strip("/")
        parts = path.split("/")
        action = parts[0] if parts else ""

        data = load_data()
        tasks = data.get("tasks", [])
        at = data.get("active_timer")

        # ── status ──────────────────────────────────────────
        if action == "status":
            by_role = {}
            for task in tasks:
                rid = task.get("role_id", "other")
                logged = task_logged_mins(task)
                live = (time.time() - at["started_at"]) / 60 if at and at.get("task_id") == task["id"] else 0
                by_role[rid] = by_role.get(rid, 0) + logged + live

            running_task = None
            if at:
                t = next((t for t in tasks if t["id"] == at.get("task_id")), None)
                if t:
                    elapsed = (time.time() - at["started_at"]) / 60
                    running_task = {"title": t["title"], "role": t.get("role_id"), "elapsed": fmt_mins(elapsed)}

            self.respond({
                "tasks": len(tasks),
                "active_timer": running_task,
                "time_by_role": {k: fmt_mins(v) for k, v in by_role.items()},
            })

        # ── timer/toggle ─────────────────────────────────────
        elif action == "timer" and len(parts) > 1 and parts[1] == "toggle":
            if at:
                # Stop running timer
                task = next((t for t in tasks if t["id"] == at["task_id"]), None)
                if task:
                    started_at = at["started_at"]
                    ended_at = time.time()
                    elapsed = (ended_at - started_at) / 60
                    if elapsed > 0.05:
                        task.setdefault("logs", []).append({
                            "id": uid(), "minutes": round(elapsed, 2),
                            "note": "Stream Deck session", "at": ended_at,
                            "started_at": started_at, "ended_at": ended_at
                        })
                data["active_timer"] = None
                save_data(data)
                self.respond({"action": "stopped", "task": task["title"] if task else "?"})
            else:
                # Start timer on most-recently-modified in-progress task
                inprogress = [t for t in tasks if t.get("status") == "inprogress"]
                if not inprogress:
                    self.respond({"error": "No in-progress tasks found"}, 404)
                    return
                target = inprogress[0]  # most recently added
                data["active_timer"] = {"task_id": target["id"], "started_at": time.time()}
                save_data(data)
                self.respond({"action": "started", "task": target["title"]})

        # ── log/<minutes> ────────────────────────────────────
        elif action == "log" and len(parts) > 1:
            try:
                mins = float(parts[1])
            except ValueError:
                self.respond({"error": "Invalid minutes"}, 400)
                return

            # Log to active timer task, or first in-progress task
            target_id = at["task_id"] if at else None
            if not target_id:
                inprogress = [t for t in tasks if t.get("status") == "inprogress"]
                if inprogress:
                    target_id = inprogress[0]["id"]

            task = next((t for t in tasks if t["id"] == target_id), None) if target_id else None
            if not task:
                self.respond({"error": "No active task to log to"}, 404)
                return

            task.setdefault("logs", []).append({
                "id": uid(), "minutes": mins,
                "note": f"Stream Deck ({int(mins)}m)", "at": time.time()
            })
            save_data(data)
            self.respond({"action": "logged", "minutes": mins, "task": task["title"]})

        # ── filter/<role> ────────────────────────────────────
        elif action == "filter":
            role = parts[1] if len(parts) > 1 else "all"
            # This just returns the filter state; the TUI reads the file
            # You can extend to write a "ui_state.json" for the TUI to watch
            self.respond({"action": "filter", "role": role, "note": "Use keyboard 1-4 in TUI to filter"})

        else:
            self.respond({"error": f"Unknown action: {action}"}, 404)


def main():
    port = 7373
    print(f"Stream Deck bridge running on http://localhost:{port}")
    print("──────────────────────────────────────────")
    print("Button URLs:")
    print(f"  Toggle timer   → http://localhost:{port}/timer/toggle")
    print(f"  Log 15 min     → http://localhost:{port}/log/15")
    print(f"  Log 30 min     → http://localhost:{port}/log/30")
    print(f"  Log 60 min     → http://localhost:{port}/log/60")
    print(f"  Status         → http://localhost:{port}/status")
    print("──────────────────────────────────────────")
    server = HTTPServer(("localhost", port), BridgeHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBridge stopped.")


if __name__ == "__main__":
    main()

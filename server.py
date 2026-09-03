#!/usr/bin/env python3
"""
Signal - local / hosted server.

Serves the dashboard, keeps the feed fresh on a background timer, and stores
read + saved state so it follows you across devices instead of living in one
browser's localStorage.

    PORT=8000 python3 server.py

Routes
    GET  /                 dashboard
    GET  /data/feed.json   generated feed
    GET  /api/state        {"read": [...], "saved": [...]}
    POST /api/state        replace read/saved sets
    GET  /api/status       generated_at, refreshing, next_refresh
    POST /api/refresh      trigger a refresh now
"""

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
FEED = os.path.join(DATA, "feed.json")
STATE = os.path.join(DATA, "state.json")

DEFAULT_REFRESH_HOURS = 3

_lock = threading.Lock()
_status = {"refreshing": False, "last_error": None, "last_run": None}
_state = {"read": [], "saved": []}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- state

def load_state():
    global _state
    try:
        with open(STATE, encoding="utf-8") as f:
            data = json.load(f)
        _state = {
            "read": list(data.get("read", [])),
            "saved": list(data.get("saved", [])),
        }
        log(f"state loaded: {len(_state['read'])} read, {len(_state['saved'])} saved")
    except (OSError, ValueError):
        _state = {"read": [], "saved": []}


def save_state():
    os.makedirs(DATA, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_state, f)
    os.replace(tmp, STATE)


# ---------------------------------------------------------------- refresh

def read_refresh_hours():
    try:
        with open(os.path.join(HERE, "sources.json"), encoding="utf-8") as f:
            return float(json.load(f).get("site", {}).get("refresh_hours", DEFAULT_REFRESH_HOURS))
    except Exception:
        return DEFAULT_REFRESH_HOURS


def feed_age_hours():
    try:
        with open(FEED, encoding="utf-8") as f:
            generated = json.load(f).get("generated_at")
        if not generated:
            return 999.0
        then = datetime.fromisoformat(generated)
        return (datetime.now(timezone.utc) - then).total_seconds() / 3600.0
    except Exception:
        return 999.0


def do_refresh():
    with _lock:
        if _status["refreshing"]:
            log("refresh already running, skipping")
            return
        _status["refreshing"] = True

    started = time.time()
    try:
        log("refreshing feed...")
        result = subprocess.run(
            [sys.executable, os.path.join(HERE, "fetch.py")],
            cwd=HERE, capture_output=True, text=True, timeout=600,
        )
        if result.returncode == 0:
            tail = [l for l in result.stdout.strip().splitlines() if l.strip()][-3:]
            log("refresh ok: " + " | ".join(tail))
            _status["last_error"] = None
        else:
            _status["last_error"] = result.stderr.strip()[-400:] or "fetch failed"
            log("refresh FAILED: " + _status["last_error"][:200])
    except Exception as exc:
        _status["last_error"] = f"{type(exc).__name__}: {exc}"
        log("refresh error: " + _status["last_error"])
    finally:
        _status["last_run"] = datetime.now(timezone.utc).isoformat()
        _status["refreshing"] = False
        log(f"refresh finished in {time.time() - started:.1f}s")


def refresh_loop():
    while True:
        hours = read_refresh_hours()
        if feed_age_hours() >= hours:
            do_refresh()
        time.sleep(300)  # re-check every 5 minutes


# ---------------------------------------------------------------- handler

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def log_message(self, fmt, *args):
        pass  # quiet, the refresh log is the interesting part

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/api/state":
            return self._json(_state)

        if path == "/api/status":
            hours = read_refresh_hours()
            age = feed_age_hours()
            return self._json({
                "generated_at": self._generated_at(),
                "age_hours": round(age, 2),
                "refresh_hours": hours,
                "next_refresh_hours": round(max(0.0, hours - age), 2),
                "refreshing": _status["refreshing"],
                "last_error": _status["last_error"],
            })

        if path in ("/", "/index.html"):
            self.path = "/index.html"

        return super().do_GET()

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"

        if path == "/api/state":
            try:
                payload = json.loads(raw.decode("utf-8"))
            except ValueError:
                return self._json({"ok": False, "error": "bad json"}, 400)
            with _lock:
                _state["read"] = list(payload.get("read", _state["read"]))
                _state["saved"] = list(payload.get("saved", _state["saved"]))
                # keep it bounded, old ids from deleted stories accumulate
                _state["read"] = _state["read"][-5000:]
                _state["saved"] = _state["saved"][-2000:]
                save_state()
            return self._json({"ok": True, **_state})

        if path == "/api/refresh":
            if _status["refreshing"]:
                return self._json({"ok": False, "error": "already refreshing"}, 409)
            threading.Thread(target=do_refresh, daemon=True).start()
            return self._json({"ok": True, "started": True})

        return self._json({"ok": False, "error": "not found"}, 404)

    def _generated_at(self):
        try:
            with open(FEED, encoding="utf-8") as f:
                return json.load(f).get("generated_at")
        except Exception:
            return None


def main():
    port = int(os.environ.get("PORT", "8000"))
    load_state()

    if feed_age_hours() >= read_refresh_hours():
        threading.Thread(target=do_refresh, daemon=True).start()
    else:
        log(f"feed is {feed_age_hours():.1f}h old, still fresh")

    threading.Thread(target=refresh_loop, daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log(f"Signal listening on 0.0.0.0:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()

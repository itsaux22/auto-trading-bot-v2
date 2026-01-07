print("Bot started")
import os
import json
from datetime import datetime, timezone

CONFIG_PATH = "config.json"
STATE_PATH = "state.json"

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r") as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def week_utc():
    now = datetime.now(timezone.utc)
    y, w, _ = now.isocalendar()
    return f"{y}-W{w}"

def main():
    config = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {})

    required = ["product_id", "usd_per_day", "max_usd_per_week", "dry_run"]
    missing = [k for k in required if k not in config]
    if missing:
        print("CONFIG ERROR: Missing keys:", ", ".join(missing))
        return

    today = today_utc()
    week = week_utc()

    if state.get("week_key") != week:
        state["week_key"] = week
        state["spent_this_week"] = 0

    if state.get("last_run_day") == today:
        print("Already ran today. Exiting.")
        return

    usd = float(config["usd_per_day"])
    max_week = float(config["max_usd_per_week"])

    if state["spent_this_week"] + usd > max_week:
        print("Weekly cap reached. No trade.")
        state["last_run_day"] = today
        save_json(STATE_PATH, state)
        return

    if config["dry_run"]:
        print(f"[DRY RUN] Would buy ${usd} of {config['product_id']}")
    else:
        print("[LIVE MODE] Coinbase trading not connected yet.")

    state["last_run_day"] = today
    if not config["dry_run"]:
        state["spent_this_week"] += usd

    save_json(STATE_PATH, state)

if __name__ == "__main__":
    main()
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

def run_bot():
    main()

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        threading.Thread(target=run_bot).start()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot triggered")

def start_server():
    server = HTTPServer(("0.0.0.0", 5000), Handler)
    server.serve_forever()

start_server()

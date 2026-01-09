import os
import json
import sys
from datetime import datetime, timezone
import urllib.request


CONFIG_PATH = "config.json"
STATE_PATH = "state.json"


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def week_utc():
    # ISO week string like "2026-W02"
    now = datetime.now(timezone.utc)
    y, w, _ = now.isocalendar()
    return f"{y}-W{w:02d}"


def get_spot_price_usd(product_id: str) -> float:
    # Coinbase public endpoint (no API key needed)
    url = f"https://api.coinbase.com/v2/prices/{product_id}/spot?currency=USD"
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return float(data["data"]["amount"])


def validate_config(cfg: dict):
    required = ["product_id", "usd_per_day", "max_usd_per_week", "dry_run"]
    missing = [k for k in required if k not in cfg]
    if missing:
        print(f"CONFIG ERROR: Missing keys: {', '.join(missing)}")
        sys.exit(1)


def main():
    print("Bot started")

    config = load_json(CONFIG_PATH, {})
    validate_config(config)

    # Default state if file doesn't exist
    state = load_json(
        STATE_PATH,
        {
            "last_run_day": None,
            "last_run_week": None,
            "spent_this_week": 0.0,
            "last_price_usd": None,
        },
    )

    # Reset weekly spend if we entered a new UTC week
    current_week = week_utc()
    if state.get("last_run_week") != current_week:
        state["spent_this_week"] = 0.0
        state["last_run_week"] = current_week

    # Optional: prevent multiple runs per day
    today = today_utc()
    if state.get("last_run_day") == today:
        print(f"Already ran today ({today}). Skipping.")
        save_json(STATE_PATH, state)
        print("Bot finished successfully.")
        return

    product_id = config["product_id"]
    usd_per_day = float(config["usd_per_day"])
    max_usd_per_week = float(config["max_usd_per_week"])
    dry_run = bool(config["dry_run"])

    # --- Fetch price ---
    try:
        current_price = get_spot_price_usd(product_id)
    except Exception as e:
        print(f"PRICE ERROR: Could not fetch price for {product_id}: {e}")
        sys.exit(1)

    print(f"Current spot price for {product_id}: ${current_price:,.2f}")

    # --- Weekly limit check ---
    spent = float(state.get("spent_this_week", 0.0))
    remaining = max_usd_per_week - spent
    print(f"Weekly spent: ${spent:.2f} / ${max_usd_per_week:.2f} (remaining ${remaining:.2f})")

    if remaining <= 0:
        print("Weekly cap reached. No buy.")
        state["last_run_day"] = today
        save_json(STATE_PATH, state)
        print("Bot finished successfully.")
        return

    # --- Decision logic (Stage 3) ---
    min_drop = float(config.get("min_drop_pct_to_buy", 0.0))
    buy_if_no_last = bool(config.get("buy_if_no_last_price", True))
    last_price = state.get("last_price_usd")

    should_buy = False
    reason = ""

    if last_price is None:
        if buy_if_no_last:
            should_buy = True
            reason = "No last_price saved yet (first run) → allowed to buy."
        else:
            should_buy = False
            reason = "No last_price saved yet (first run) → configured to skip."
    else:
        last_price = float(last_price)
        drop_pct = (last_price - current_price) / last_price * 100.0
        if drop_pct >= min_drop:
            should_buy = True
            reason = f"Price dropped {drop_pct:.2f}% (>= {min_drop:.2f}%) → buy condition met."
        else:
            should_buy = False
            reason = f"Price dropped {drop_pct:.2f}% (< {min_drop:.2f}%) → skip."

    print("Decision:", reason)

    # Always store latest price so the next run can compare
    state["last_price_usd"] = current_price

    # Cap today's buy by remaining weekly budget
    usd_to_use = min(usd_per_day, remaining)

    if should_buy and usd_to_use > 0:
        if dry_run:
            print(f"[DRY RUN] Would buy ${usd_to_use:.2f} of {product_id}")
        else:
            print("[LIVE MODE] Trading not connected yet.")
        # Only count spend when not dry_run
        if not dry_run:
            state["spent_this_week"] = float(state.get("spent_this_week", 0.0)) + usd_to_use
    else:
        print("No buy today.")

    # Mark run day + save state
    state["last_run_day"] = today
    state["last_run_week"] = current_week
    save_json(STATE_PATH, state)

    print("Bot finished successfully.")


if __name__ == "__main__":
    main()




import os
import json
import sys
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

CONFIG_PATH = "config.json"
STATE_PATH = "state.json"


# ----------------------------
# Helpers
# ----------------------------
def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def week_id_utc() -> str:
    # ISO week id like "2026-W02"
    now = datetime.now(timezone.utc)
    iso_year, iso_week, _ = now.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def pct_drop(old_price: float, new_price: float) -> float:
    if old_price <= 0:
        return 0.0
    return ((old_price - new_price) / old_price) * 100.0


def get_coinbase_spot_price(product_id: str) -> float:
    """
    Uses Coinbase public spot endpoint (no auth needed).
    Example: https://api.coinbase.com/v2/prices/BTC-USD/spot
    """
    url = f"https://api.coinbase.com/v2/prices/{product_id}/spot"
    req = Request(url, headers={"User-Agent": "auto-trading-bot-v2"})
    with urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw)
    amount = data["data"]["amount"]
    return float(amount)


# ----------------------------
# Main Bot
# ----------------------------
def main():
    print("Bot started")

    # ---- Load config
    config = load_json(CONFIG_PATH, {})
    required = ["product_id", "usd_per_day", "max_usd_per_week", "dry_run"]
    missing = [k for k in required if k not in config]

    if missing:
        print(f"CONFIG ERROR: Missing keys: {', '.join(missing)}")
        print(f"Fix {CONFIG_PATH} and re-run.")
        return 0  # don't crash actions

    product_id = str(config["product_id"]).strip()
    usd_per_day = float(config["usd_per_day"])
    max_usd_per_week = float(config["max_usd_per_week"])
    dry_run = bool(config["dry_run"])

    # Optional knobs
    min_drop_pct_to_buy = float(config.get("min_drop_pct_to_buy", 0.0))
    buy_if_no_last_price = bool(config.get("buy_if_no_last_price", True))

    # ---- Load state (robust defaults)
    state = load_json(
        STATE_PATH,
        {
            "last_run_day": "",
            "week": "",
            "spent_this_week": 0.0,
            "last_price": None,
        },
    )

    # Make sure keys exist even if state.json is older/broken
    state.setdefault("last_run_day", "")
    state.setdefault("week", "")
    state.setdefault("spent_this_week", 0.0)
    state.setdefault("last_price", None)

    today = today_utc()
    week = week_id_utc()

    # ---- Weekly reset
    if state["week"] != week:
        state["week"] = week
        state["spent_this_week"] = 0.0

    # ---- One run per day
    if state["last_run_day"] == today:
        print(f"Already ran today ({today}). Exiting.")
        save_json(STATE_PATH, state)
        return 0

    # ---- Get live price
    try:
        spot = get_coinbase_spot_price(product_id)
    except (HTTPError, URLError, TimeoutError) as e:
        print(f"PRICE ERROR: Could not fetch spot price for {product_id}: {e}")
        return 0
    except Exception as e:
        print(f"PRICE ERROR: Unexpected error: {e}")
        return 0

    print(f"Current spot price for {product_id}: ${spot:,.2f}")

    # ---- Budget checks
    spent = float(state["spent_this_week"])
    remaining_week = max(0.0, max_usd_per_week - spent)
    buy_usd = min(usd_per_day, remaining_week)

    print(f"Weekly spent: ${spent:,.2f} / ${max_usd_per_week:,.2f} (remaining ${remaining_week:,.2f})")

    if buy_usd <= 0:
        print("Decision: Weekly budget exhausted -> no buy")
        state["last_run_day"] = today
        save_json(STATE_PATH, state)
        print("Bot finished successfully.")
        return 0

    # ---- Decision logic
    last_price = state.get("last_price", None)

    should_buy = False
    reason = ""

    if last_price is None:
        if buy_if_no_last_price:
            should_buy = True
            reason = "No last_price yet -> buy allowed"
        else:
            should_buy = False
            reason = "No last_price yet -> buy not allowed"
    else:
        drop = pct_drop(float(last_price), spot)
        if drop >= min_drop_pct_to_buy:
            should_buy = True
            reason = f"Price dropped {drop:.2f}% (>= {min_drop_pct_to_buy:.2f}%)"
        else:
            should_buy = False
            reason = f"Price dropped {drop:.2f}% (< {min_drop_pct_to_buy:.2f}%)"

    print(f"Decision: {reason}")

    # ---- Execute (DRY RUN vs LIVE)
    if should_buy:
        if dry_run:
            print(f"[DRY RUN] Would buy ${buy_usd:.2f} of {product_id}")
        else:
            # Stage 6 will connect real trading here
            print("[LIVE MODE] Trading not connected yet. (Stage 6)")
            print(f"[LIVE MODE] Would buy ${buy_usd:.2f} of {product_id}")

            # If/when you wire live trading, update weekly spent:
            # state["spent_this_week"] = float(state["spent_this_week"]) + buy_usd
    else:
        print("No buy today.")

    # ---- Update state
    state["last_price"] = spot
    state["last_run_day"] = today

    save_json(STATE_PATH, state)
    print("Bot finished successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

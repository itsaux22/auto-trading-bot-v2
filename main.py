import os
import json
from datetime import datetime, timezone

CONFIG_PATH = "config.json"
STATE_PATH = "state.json"


# ------------------------
# Helpers
# ------------------------

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
    return f"{now.year}-W{now.isocalendar().week}"


# ------------------------
# Main bot logic
# ------------------------

def main():
    print("Bot started")

    # Load config
    config = load_json(CONFIG_PATH, {})
    required_keys = [
        "product_id",
        "usd_per_day",
        "max_usd_per_week",
        "dry_run",
        "min_drop_pct_to_buy",
        "buy_if_no_last_price",
    ]

    missing = [k for k in required_keys if k not in config]
    if missing:
        print(f"CONFIG ERROR: Missing keys: {', '.join(missing)}")
        return

    # Load state
    state = load_json(
        STATE_PATH,
        {
            "last_run_day": None,
            "week": None,
            "spent_this_week": 0.0,
            "last_price": None,
        },
    )

    today = today_utc()
    week = week_utc()

    # Reset weekly spend if new week
    if state["week"] != week:
        state["week"] = week
        state["spent_this_week"] = 0.0

    # Prevent multiple runs per day
    if state["last_run_day"] == today:
        print("Already ran today. Skipping.")
        return

    usd = float(config["usd_per_day"])

    if state["spent_this_week"] + usd > config["max_usd_per_week"]:
        print("Weekly limit reached. Skipping buy.")
        return

    # Read Coinbase secrets (Stage 4)
    api_key = os.getenv("COINBASE_API_KEY")
    api_secret = os.getenv("COINBASE_API_SECRET")

    if api_key and api_secret:
        print("[STAGE 4] Coinbase API keys detected.")
    else:
        print("[WARNING] Coinbase API keys NOT found.")
        print("Bot will remain in DRY RUN mode.")

    # ------------------------
    # Price logic (stub for now)
    # ------------------------

    current_price = None  # Stage 5 will fetch real price

    should_buy = False

    if current_price is None:
        if state["last_price"] is None and config["buy_if_no_last_price"]:
            should_buy = True
        else:
            print("No price data available. Skipping buy.")
    else:
        drop_pct = ((state["last_price"] - current_price) / state["last_price"]) * 100
        if drop_pct >= config["min_drop_pct_to_buy"]:
            should_buy = True

    # ------------------------
    # Execute (or simulate) trade
    # ------------------------

    if should_buy:
        if config["dry_run"]:
            print(
                f"[DRY RUN] Would buy ${usd} of {config['product_id']}"
            )
        else:
            print(
                f"[LIVE MODE] Buying ${usd} of {config['product_id']} (NOT IMPLEMENTED YET)"
            )

        state["spent_this_week"] += usd
    else:
        print("Buy conditions not met.")

    # Save state
    state["last_run_day"] = today
    if current_price is not None:
        state["last_price"] = current_price

    save_json(STATE_PATH, state)

    print("Bot finished successfully.")


# ------------------------
# Entry point
# ------------------------

if __name__ == "__main__":
    main()

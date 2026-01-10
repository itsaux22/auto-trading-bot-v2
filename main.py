import os
import json
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
    now = datetime.now(timezone.utc)
    y, w, _ = now.isocalendar()
    return f"{y}-W{w:02d}"


def get_spot_price_usd(product_id: str) -> float:
    url = f"https://api.coinbase.com/v2/prices/{product_id}/spot?currency=USD"
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return float(data["data"]["amount"])


def validate_config(cfg: dict):
    required = ["product_id", "usd_per_day", "max_usd_per_week", "dry_run", "min_drop_pct_to_buy", "buy_if_no_last_price"]
    missing = [k for k in required if k not in cfg]
    if missing:
        print(f"CONFIG ERROR: Missing keys: {', '.join(missing)}")
        raise SystemExit(1)


def coinbase_readonly_check():
    """
    Stage 5: Read-only check using Coinbase Advanced SDK:
    - Lists accounts
    - Prints a few balances
    """
    api_key = os.getenv("COINBASE_API_KEY", "").strip()
    api_secret = os.getenv("COINBASE_API_SECRET", "").strip()

    if not api_key or not api_secret:
        print("[STAGE 5] Coinbase keys NOT found in env. (Set GitHub Secrets COINBASE_API_KEY / COINBASE_API_SECRET)")
        return

    # SDK expects CDP keys. Secret usually looks like a PEM (-----BEGIN ...-----)
    if "BEGIN" not in api_secret:
        print("[STAGE 5] Coinbase secret does NOT look like a PEM private key.")
        print("         You probably need a CDP Advanced Trade API key (ECDSA) for this SDK.")
        return

    print("[STAGE 5] Coinbase keys detected. Trying read-only account list...")

    try:
        from coinbase.rest import RESTClient
        client = RESTClient(api_key=api_key, api_secret=api_secret)

        # List Accounts (read-only)
        resp = client.get_accounts()
        accounts = resp.get("accounts", [])

        print(f"[STAGE 5] Accounts returned: {len(accounts)}")
        # Print top 10 balances
        shown = 0
        for a in accounts:
            bal = (a.get("available_balance") or {})
            val = bal.get("value")
            cur = bal.get("currency")
            name = a.get("name")
            if val is None or cur is None:
                continue
            print(f"  - {name}: {val} {cur}")
            shown += 1
            if shown >= 10:
                break

        print("[STAGE 5] Read-only check OK.")

    except Exception as e:
        print("[STAGE 5] Read-only check FAILED.")
        print("Error:", str(e))


def main():
    print("Bot started")

    config = load_json(CONFIG_PATH, {})
    validate_config(config)

    state = load_json(
        STATE_PATH,
        {
            "last_run_day": None,
            "last_run_week": None,
            "spent_this_week": 0.0,
            "last_price_usd": None,
        },
    )

    # --- Stage 5 read-only Coinbase verification ---
    coinbase_readonly_check()

    # --- existing Stage 3 decision logic (price drop) ---
    current_week = week_utc()
    if state.get("last_run_week") != current_week:
        state["spent_this_week"] = 0.0
        state["last_run_week"] = current_week

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

    current_price = get_spot_price_usd(product_id)
    print(f"Current spot price for {product_id}: ${current_price:,.2f}")

    spent = float(state.get("spent_this_week", 0.0))
    remaining = max_usd_per_week - spent
    print(f"Weekly spent: ${spent:.2f} / ${max_usd_per_week:.2f} (remaining ${remaining:.2f})")

    if remaining <= 0:
        print("Weekly cap reached. No buy.")
        state["last_run_day"] = today
        save_json(STATE_PATH, state)
        print("Bot finished successfully.")
        return

    min_drop = float(config.get("min_drop_pct_to_buy", 0.0))
    buy_if_no_last = bool(config.get("buy_if_no_last_price", True))
    last_price = state.get("last_price_usd")

    should_buy = False
    if last_price is None:
        should_buy = buy_if_no_last
        print("Decision:", "No last_price yet → " + ("buy allowed" if should_buy else "skip"))
    else:
        last_price = float(last_price)
        drop_pct = (last_price - current_price) / last_price * 100.0
        if drop_pct >= min_drop:
            should_buy = True
            print("Decision:", f"Price dropped {drop_pct:.2f}% (>= {min_drop:.2f}%) → BUY")
        else:
            print("Decision:", f"Price dropped {drop_pct:.2f}% (< {min_drop:.2f}%) → SKIP")

    state["last_price_usd"] = current_price

    usd_to_use = min(usd_per_day, remaining)
    if should_buy and usd_to_use > 0:
        if dry_run:
            print(f"[DRY RUN] Would buy ${usd_to_use:.2f} of {product_id}")
        else:
            print("[LIVE MODE] Trading not connected yet (Stage 6).")
        if not dry_run:
            state["spent_this_week"] = float(state.get("spent_this_week", 0.0)) + usd_to_use
    else:
        print("No buy today.")

    state["last_run_day"] = today
    state["last_run_week"] = current_week
    save_json(STATE_PATH, state)

    print("Bot finished successfully.")


if __name__ == "__main__":
    main()

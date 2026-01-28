import os
import json
import time
import math
import uuid
from datetime import datetime, timezone, timedelta
from coinbase.rest import RESTClient

CONFIG_PATH = "config.json"
STATE_PATH = "state.json"

# -------------------------
# Utilities
# -------------------------
def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r") as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def now():
    return datetime.now(timezone.utc)

def week_key():
    y, w, _ = now().isocalendar()
    return f"{y}-W{w}"

def pct_drop(new, old):
    if old <= 0:
        return 0
    return (old - new) / old * 100

# -------------------------
# Volatility (7-day)
# -------------------------
def get_7d_volatility(client, product_id):
    end = int(time.time())
    start = end - 7 * 24 * 3600

    candles = client.get_candles(
        product_id=product_id,
        start=start,
        end=end,
        granularity="ONE_HOUR"
    )["candles"]

    closes = [float(c["close"]) for c in candles if float(c["close"]) > 0]
    returns = [
        math.log(closes[i] / closes[i-1])
        for i in range(1, len(closes))
    ]

    if len(returns) < 10:
        return None

    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    hourly_vol = math.sqrt(variance)

    return hourly_vol * math.sqrt(24) * 100  # % per day

# -------------------------
# Main Bot
# -------------------------
def main():
    print("Bot started")

    cfg = load_json(CONFIG_PATH, {})
    required = ["product_id", "usd_per_day", "max_usd_per_week", "dry_run"]
    for k in required:
        if k not in cfg:
            print(f"CONFIG ERROR: Missing {k}")
            return

    state = load_json(STATE_PATH, {
        "week": None,
        "weekly_spent": 0,
        "last_price": None,
        "last_buy_ts": 0,
        "last_run": None
    })

    if state["last_run"] == now().date().isoformat():
        print("Already ran today. Exiting.")
        return

    state["last_run"] = now().date().isoformat()

    if state["week"] != week_key():
        state["week"] = week_key()
        state["weekly_spent"] = 0

    client = RESTClient(
        api_key=os.environ["COINBASE_API_KEY"],
        api_secret=os.environ["COINBASE_API_SECRET"]
    )

    accounts = client.get_accounts()["accounts"]
    print(f"[STAGE 5] Read-only OK. accounts_count={len(accounts)}")

    price = float(client.get_product(cfg["product_id"])["price"])
    print(f"Current price: ${price:,.2f}")

    vol = get_7d_volatility(client, cfg["product_id"])
    dip_required = cfg.get("min_drop_pct_to_buy", 1.0)

    if vol:
        if vol < 1.5:
            dip_required *= 0.7
        elif vol > 3.0:
            dip_required *= 1.7

    print(f"Dip required: {dip_required:.2f}%")

    if state["weekly_spent"] >= cfg["max_usd_per_week"]:
        print("Weekly cap reached")
        save_json(STATE_PATH, state)
        return

    if state["last_buy_ts"] and time.time() - state["last_buy_ts"] < 12 * 3600:
        print("Cooldown active")
        save_json(STATE_PATH, state)
        return

    should_buy = False
    if state["last_price"] is None:
        should_buy = cfg.get("buy_if_no_last_price", True)
    else:
        drop = pct_drop(price, state["last_price"])
        should_buy = drop >= dip_required

    if not should_buy:
        print("Decision: SKIP")
        state["last_price"] = price
        save_json(STATE_PATH, state)
        return

    usd = min(cfg["usd_per_day"], cfg["max_usd_per_week"] - state["weekly_spent"])

    if cfg["dry_run"]:
        print(f"[DRY RUN] Would buy ${usd}")
    else:
        print("[LIVE] Placing order")
        client.create_order(
            client_order_id=str(uuid.uuid4()),
            product_id=cfg["product_id"],
            side="BUY",
            order_configuration={
                "market_market_ioc": {"quote_size": f"{usd:.2f}"}
            }
        )

    state["weekly_spent"] += usd
    state["last_price"] = price
    state["last_buy_ts"] = int(time.time())
    save_json(STATE_PATH, state)

    print("Bot finished successfully")

if __name__ == "__main__":
    main()
raise

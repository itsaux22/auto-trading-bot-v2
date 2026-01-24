import json
import time
import os
import math
import requests
from datetime import datetime, timezone

from coinbase.rest import RESTClient


# =========================
# Helpers
# =========================

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses += abs(diff)
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))


def fetch_candles(product_id, granularity_sec, limit):
    url = f"https://api.exchange.coinbase.com/products/{product_id}/candles"
    params = {"granularity": granularity_sec}
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()[:limit]
    data.reverse()
    candles = []
    for t, low, high, o, c, v in data:
        candles.append({"close": c, "high": high})
    return candles


# =========================
# Entry Logic (Stage A)
# =========================

def should_buy_dip_and_trend(cfg, spot_price):
    candles = fetch_candles(
        cfg["product_id"],
        cfg["candle_granularity_sec"],
        cfg["lookback_candles"],
    )

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]

    sma_short = sma(closes, cfg["sma_short"])
    sma_long = sma(closes, cfg["sma_long"])
    r = rsi(closes, cfg["rsi_period"])

    if sma_short is None or sma_long is None or r is None:
        return False, "Not enough candle data"

    if cfg["require_uptrend"] and not (sma_short > sma_long):
        return False, f"Trend DOWN (SMA{cfg['sma_short']} <= SMA{cfg['sma_long']})"

    if r > cfg["max_rsi_to_buy"]:
        return False, f"RSI too high ({r:.1f})"

    dip_level = sma_short * (1 - cfg["dip_below_sma_short_pct"] / 100)
    if spot_price > dip_level:
        return False, "Not a dip yet"

    recent_high = max(highs)
    drop_pct = (recent_high - spot_price) / recent_high * 100
    if drop_pct < cfg["min_intraday_drop_pct"]:
        return False, "No meaningful pullback"

    return True, f"BUY OK | RSI={r:.1f} | Dip confirmed"


# =========================
# Main
# =========================

def main():
    print("Bot started")

    cfg = load_json("config.json", {})
    required = [
        "product_id",
        "usd_per_day",
        "max_usd_per_week",
        "dry_run",
    ]
    missing = [k for k in required if k not in cfg]
    if missing:
        print(f"CONFIG ERROR: Missing keys: {', '.join(missing)}")
        return

    state = load_json("state.json", {})
    now = datetime.now(timezone.utc)

    # Prevent double runs same day
    today = now.strftime("%Y-%m-%d")
    if state.get("last_run_date") == today:
        print(f"Already ran today ({today}). Exiting.")
        return
    state["last_run_date"] = today

    client = RESTClient(
        api_key=os.environ["COINBASE_API_KEY"],
        api_secret=os.environ["COINBASE_API_SECRET"],
    )

    # =========================
    # Stage 5: Read-only check
    # =========================
    try:
        accounts = client.get_accounts()
        print(f"[STAGE 5] Read-only check OK. accounts_count={len(accounts.accounts)}")
    except Exception as e:
        print(f"[STAGE 5] Read-only check FAILED: {e}")

    # Spot price
    product = client.get_product(cfg["product_id"])
    spot_price = float(product.price)
    print(f"Current spot price for {cfg['product_id']}: ${spot_price:,.2f}")

    # Weekly spend tracking
    week = now.strftime("%Y-W%U")
    if state.get("week") != week:
        state["week"] = week
        state["weekly_spent"] = 0.0

    weekly_spent = state.get("weekly_spent", 0.0)
    print(
        f"Weekly spent: ${weekly_spent:.2f} / ${cfg['max_usd_per_week']:.2f}"
    )

    if weekly_spent + cfg["usd_per_day"] > cfg["max_usd_per_week"]:
        print("Weekly cap reached. Skipping.")
        save_json("state.json", state)
        return

    # Cooldown
    last_buy_ts = state.get("last_buy_ts")
    if last_buy_ts:
        minutes_since = (time.time() - last_buy_ts) / 60
        if minutes_since < cfg["min_minutes_between_buys"]:
            print("Cooldown active. Skipping.")
            save_json("state.json", state)
            return

    # =========================
    # Entry decision
    # =========================
    decision, reason = should_buy_dip_and_trend(cfg, spot_price)
    print(f"Decision: {'BUY' if decision else 'SKIP'} | {reason}")

    if not decision:
        save_json("state.json", state)
        return

    # =========================
    # Stage 6: Execute order
    # =========================
    if cfg["dry_run"]:
        print(f"[DRY RUN] Would buy ${cfg['usd_per_day']} of {cfg['product_id']}")
    else:
        print("[STAGE 6] LIVE MODE enabled. Placing real order...")
        order = client.create_market_order(
            product_id=cfg["product_id"],
            side="BUY",
            quote_size=str(cfg["usd_per_day"]),
        )
        print("[STAGE 6] Order placed.")
        print(f"[STAGE 6] client_order_id={order.client_order_id}")

        state["weekly_spent"] = weekly_spent + cfg["usd_per_day"]
        state["last_buy_ts"] = time.time()

    save_json("state.json", state)
    print("Bot finished successfully.")


if __name__ == "__main__":
    main()

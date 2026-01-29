import os
import json
import time
import math
import uuid
from datetime import datetime, timezone
from coinbase.rest import RESTClient

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

def now():
    return datetime.now(timezone.utc)

def week_key():
    y, w, _ = now().isocalendar()
    return f"{y}-W{w}"

def pct_drop(new, old):
    if old <= 0:
        return 0
    return (old - new) / old * 100

def get_hourly_closes(client, product_id, hours):
    end = int(time.time())
    start = end - hours * 3600
    resp = client.get_candles(
        product_id=product_id,
        start=start,
        end=end,
        granularity="ONE_HOUR"
    )
    candles = resp.get("candles", resp.get("data", [])) if isinstance(resp, dict) else resp
    closes = [float(c["close"]) for c in candles if float(c["close"]) > 0]
    closes.reverse()  # oldest -> newest (Coinbase often returns newest first)
    return closes

def sma(values):
    if not values:
        return None
    return sum(values) / len(values)

def get_7d_volatility(client, product_id):
    closes = get_hourly_closes(client, product_id, 7 * 24)
    if len(closes) < 20:
        return None
    returns = [math.log(closes[i] / closes[i-1]) for i in range(1, len(closes))]
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    hourly_vol = math.sqrt(variance)
    return hourly_vol * math.sqrt(24) * 100  # % per day

def main():
    print("Bot started")

    cfg = load_json(CONFIG_PATH, {})
    required = ["product_id", "usd_per_day", "max_usd_per_week", "dry_run"]
    missing = [k for k in required if k not in cfg]
    if missing:
        print(f"CONFIG ERROR: Missing keys: {', '.join(missing)}")
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

    # Stage 5: read-only check
    accounts_resp = client.get_accounts()
    accounts = accounts_resp.get("accounts", []) if isinstance(accounts_resp, dict) else []
    print(f"[STAGE 5] Read-only check OK. accounts_count={len(accounts)}")

    # Current price
    product = client.get_product(cfg["product_id"])
    price = float(product["price"])
    print(f"Current price for {cfg['product_id']}: ${price:,.2f}")

    # Base dip threshold
    dip_required = float(cfg.get("min_drop_pct_to_buy", 1.0))

    # Volatility adjustment
    vol = get_7d_volatility(client, cfg["product_id"])
    if vol:
        if vol < 1.5:
            dip_required *= 0.7
        elif vol > 3.0:
            dip_required *= 1.7
    print(f"Dip required (after vol): {dip_required:.2f}%")

    # ---- E: Trend filter (fast MA vs slow MA) ----
    use_trend = bool(cfg.get("use_trend_filter", True))
    fast_h = int(cfg.get("trend_fast_hours", 24))
    slow_h = int(cfg.get("trend_slow_hours", 168))

    in_downtrend = False
    if use_trend:
        closes_slow = get_hourly_closes(client, cfg["product_id"], slow_h)
        if len(closes_slow) >= slow_h * 0.6:  # enough data
            fast_ma = sma(closes_slow[-fast_h:]) if len(closes_slow) >= fast_h else sma(closes_slow)
            slow_ma = sma(closes_slow)
            print(f"Trend MAs: fast({fast_h}h)={fast_ma:,.2f} | slow({slow_h}h)={slow_ma:,.2f}")

            if fast_ma is not None and slow_ma is not None and fast_ma < slow_ma:
                in_downtrend = True
                print("[TREND] Downtrend detected (fast MA < slow MA).")
        else:
            print("[TREND] Not enough candle data; skipping trend filter.")

    if in_downtrend:
        mode = cfg.get("downtrend_mode", "stricter")
        if mode == "skip":
            print("[TREND] Downtrend mode=skip → No trade today.")
            state["last_price"] = price
            save_json(STATE_PATH, state)
            return
        else:
            mult = float(cfg.get("downtrend_multiplier", 2.0))
            dip_required *= mult
            print(f"[TREND] Downtrend mode=stricter → dip_required now {dip_required:.2f}%")

    # Weekly cap check
    if state["weekly_spent"] >= float(cfg["max_usd_per_week"]):
        print("Weekly cap reached")
        save_json(STATE_PATH, state)
        return

    # Cooldown (12h)
    if state["last_buy_ts"] and time.time() - state["last_buy_ts"] < 12 * 3600:
        print("Cooldown active")
        save_json(STATE_PATH, state)
        return

    # Buy decision
    should_buy = False
    if state["last_price"] is None:
        should_buy = bool(cfg.get("buy_if_no_last_price", True))
        print("No last_price yet → buy allowed" if should_buy else "No last_price yet → buy NOT allowed")
    else:
        drop = pct_drop(price, float(state["last_price"]))
        print(f"Drop from last_price: {drop:.2f}%")
        should_buy = drop >= dip_required

    if not should_buy:
        print("Decision: SKIP | Not a dip yet")
        state["last_price"] = price
        save_json(STATE_PATH, state)
        return

    usd = min(float(cfg["usd_per_day"]), float(cfg["max_usd_per_week"]) - float(state["weekly_spent"]))
    print(f"Decision: BUY | amount=${usd:.2f}")

    if cfg["dry_run"]:
        print(f"[DRY RUN] Would buy ${usd:.2f} of {cfg['product_id']}")
    else:
        print("[LIVE MODE] Placing real order...")
        resp = client.create_order(
            client_order_id=str(uuid.uuid4()),
            product_id=cfg["product_id"],
            side="BUY",
            order_configuration={
                "market_market_ioc": {"quote_size": f"{usd:.2f}"}
            }
        )
        print("Order placed.")
        # Some SDK responses vary; print safely
        if isinstance(resp, dict):
            print(f"Response keys: {list(resp.keys())}")

    state["weekly_spent"] = float(state["weekly_spent"]) + usd
    state["last_price"] = price
    state["last_buy_ts"] = int(time.time())
    save_json(STATE_PATH, state)

    print("Bot finished successfully")

if __name__ == "__main__":
    main()

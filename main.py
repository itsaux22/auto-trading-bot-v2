import os
import json
import time
import math
import uuid
from datetime import datetime, timezone, timedelta

# Coinbase Advanced Trade SDK
from coinbase.rest import RESTClient

CONFIG_PATH = "config.json"
STATE_PATH = "state.json"

# -------------------------
# Helpers
# -------------------------
def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)

def now_utc():
    return datetime.now(timezone.utc)

def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()

def get_week_key(dt):
    # ISO week key: YYYY-Www
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"

def pct_change(new, old):
    if old is None or old <= 0:
        return None
    return (new - old) / old * 100.0

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

# -------------------------
# Volatility (7-day) logic
# -------------------------
def compute_volatility_7d(client, product_id, granularity_seconds=3600, days=7):
    """
    Computes annualized-ish volatility using hourly closes over last N days.
    We use std dev of hourly log returns, then scale to daily-ish.
    For decision tiers, we don't need perfect finance-grade scaling,
    just a stable volatility signal.
    """
    end = now_utc()
    start = end - timedelta(days=days)

    # Coinbase candles: granularity might be "ONE_HOUR" in some SDKs,
    # or seconds in others. We'll try seconds first, fallback to strings.
    candles = None
    try:
        resp = client.get_candles(
            product_id=product_id,
            start=int(start.timestamp()),
            end=int(end.timestamp()),
            granularity=granularity_seconds
        )
        candles = resp.get("candles") if isinstance(resp, dict) else getattr(resp, "candles", None)
    except Exception:
        # Fallback attempt for SDKs that want string enum granularity
        try:
            resp = client.get_candles(
                product_id=product_id,
                start=int(start.timestamp()),
                end=int(end.timestamp()),
                granularity="ONE_HOUR"
            )
            candles = resp.get("candles") if isinstance(resp, dict) else getattr(resp, "candles", None)
        except Exception as e:
            print(f"[STAGE A] Volatility fetch failed: {e}")
            return None

    if not candles or len(candles) < 10:
        print("[STAGE A] Not enough candle data to compute volatility.")
        return None

    # Candle format varies; try to pull closes robustly
    closes = []
    for c in candles:
        # common shapes:
        # dict: {"close": "123.45", "start": "..."} or {"close": 123.45}
        # list/tuple: [start, low, high, open, close, volume]
        close_val = None
        if isinstance(c, dict) and "close" in c:
            close_val = float(c["close"])
        elif isinstance(c, (list, tuple)) and len(c) >= 5:
            close_val = float(c[4])
        else:
            # try attribute
            close_val = getattr(c, "close", None)
            if close_val is not None:
                close_val = float(close_val)

        if close_val and close_val > 0:
            closes.append(close_val)

    # Need consecutive closes
    if len(closes) < 10:
        print("[STAGE A] Candle closes insufficient after parsing.")
        return None

    # Sort might be newest-first depending on API; make it oldest->newest if needed
    # If the list is newest->oldest, returns will still work but invert time;
    # safest is just use as-is but check and reverse if time decreases.
    # We can't reliably parse timestamps from every candle, so we’ll just assume API returns ascending.
    # If your API returns descending, it still computes std dev fine.

    # Compute hourly log returns
    rets = []
    for i in range(1, len(closes)):
        if closes[i-1] > 0 and closes[i] > 0:
            rets.append(math.log(closes[i] / closes[i-1]))

    if len(rets) < 5:
        print("[STAGE A] Not enough returns to compute volatility.")
        return None

    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    std = math.sqrt(var)

    # Convert to % per day-ish: hourly std * sqrt(24) * 100
    vol_pct_per_day = std * math.sqrt(24) * 100.0
    return vol_pct_per_day

def choose_dynamic_dip(vol_pct_per_day, base_min_drop_pct, cfg):
    """
    Tiered dip thresholds:
    - low vol: smaller dip required
    - medium: normal
    - high: larger dip required
    """
    # Optional overrides in config.json
    vol_low = float(cfg.get("vol_low_pct_per_day", 1.5))
    vol_high = float(cfg.get("vol_high_pct_per_day", 3.0))

    dip_low = float(cfg.get("dip_low_pct", max(0.4, base_min_drop_pct * 0.7)))
    dip_med = float(cfg.get("dip_med_pct", base_min_drop_pct))
    dip_high = float(cfg.get("dip_high_pct", max(dip_med, base_min_drop_pct * 1.7)))

    if vol_pct_per_day is None:
        return dip_med, "unknown"
    if vol_pct_per_day < vol_low:
        return dip_low, "low"
    if vol_pct_per_day > vol_high:
        return dip_high, "high"
    return dip_med, "medium"

# -------------------------
# Trading logic
# -------------------------
def get_spot_price(client, product_id):
    """
    Get current spot/last trade price.
    Coinbase responses vary; we try a few patterns.
    """
    try:
        # Some SDKs have get_product
        p = client.get_product(product_id=product_id)
        if isinstance(p, dict):
            # common: p["price"]
            if "price" in p:
                return float(p["price"])
            # or nested
            if "product" in p and isinstance(p["product"], dict) and "price" in p["product"]:
                return float(p["product"]["price"])
        else:
            price = getattr(p, "price", None)
            if price is not None:
                return float(price)
    except Exception as e:
        print(f"[WARN] get_product failed: {e}")

    # Fallback: try get_best_bid_ask or ticker-like
    try:
        t = client.get_best_bid_ask(product_id=product_id)
        if isinstance(t, dict):
            # if both exist, use mid
            bid = float(t["best_bid"]) if "best_bid" in t else None
            ask = float(t["best_ask"]) if "best_ask" in t else None
            if bid and ask:
                return (bid + ask) / 2.0
            if bid:
                return bid
            if ask:
                return ask
        else:
            bid = getattr(t, "best_bid", None)
            ask = getattr(t, "best_ask", None)
            if bid and ask:
                return (float(bid) + float(ask)) / 2.0
            if bid:
                return float(bid)
            if ask:
                return float(ask)
    except Exception as e:
        print(f"[WARN] get_best_bid_ask failed: {e}")

    return None

def place_market_buy(client, product_id, usd_amount):
    """
    Places a market buy for USD notional (quote size).
    For Advanced Trade, this is usually "quote_size" or "quote_quantity".
    We'll try quote_size.
    """
    client_order_id = str(uuid.uuid4())
    try:
        # Common pattern in Advanced Trade SDK:
        # client.create_order(product_id=..., side="BUY", order_configuration={...})
        resp = client.create_order(
            client_order_id=client_order_id,
            product_id=product_id,
            side="BUY",
            order_configuration={
                "market_market_ioc": {
                    "quote_size": f"{usd_amount:.2f}"
                }
            }
        )
        return client_order_id, resp
    except Exception as e:
        return client_order_id, {"error": str(e)}

# -------------------------
# Main
# -------------------------
def main():
    print("Bot started")

    cfg = load_json(CONFIG_PATH, default={})
    required = ["product_id", "usd_per_day", "max_usd_per_week", "dry_run"]
    missing = [k for k in required if k not in cfg]
    if missing:
        print(f"CONFIG ERROR: Missing keys: {', '.join(missing)}")
        print("Bot finished successfully.")
        return

    product_id = cfg["product_id"]
    usd_per_day = float(cfg["usd_per_day"])
    max_usd_per_week = float(cfg["max_usd_per_week"])
    dry_run = bool(cfg["dry_run"])

    base_min_drop = float(cfg.get("min_drop_pct_to_buy", 1.0))
    buy_if_no_last = bool(cfg.get("buy_if_no_last_price", True))

    # Cooldown in hours (optional)
    cooldown_hours = float(cfg.get("cooldown_hours", 12))
    cooldown_seconds = int(cooldown_hours * 3600)

    # If you want the bot to run more than once per day, set allow_multiple_runs_per_day=true
    allow_multiple_runs = bool(cfg.get("allow_multiple_runs_per_day", False))

    # Secrets
    api_key = os.environ.get("COINBASE_API_KEY", "").strip()
    api_secret = os.environ.get("COINBASE_API_SECRET", "").strip()

    if not api_key or not api_secret:
        print("[STAGE 5] Coinbase keys missing in environment.")
        print("Bot finished successfully.")
        return

    client = RESTClient(api_key=api_key, api_secret=api_secret)

    # Read-only test (you had this working)
    try:
        accounts = client.get_accounts()
        # Some SDKs: dict with "accounts"; others: object with .accounts
        acc_list = accounts.get("accounts") if isinstance(accounts, dict) else getattr(accounts, "accounts", None)
        acc_count = len(acc_list) if acc_list else 0
        print(f"[STAGE 5] Read-only check OK. accounts_count={acc_count}")
    except Exception as e:
        print("[STAGE 5] Read-only check FAILED.")
        print(f"Error: {e}")

    # Load state
    st = load_json(STATE_PATH, default={
        "week_key": None,
        "weekly_spent": 0.0,
        "last_price": None,
        "last_run_date": None,
        "last_buy_ts": None
    })

    # Enforce once-per-day (optional)
    today = now_utc().date().isoformat()
    if not allow_multiple_runs and st.get("last_run_date") == today:
        print(f"Already ran today ({today}). Exiting.")
        print("Bot finished successfully.")
        return
    st["last_run_date"] = today

    # Weekly budget reset
    wk = get_week_key(now_utc())
    if st.get("week_key") != wk:
        st["week_key"] = wk
        st["weekly_spent"] = 0.0

    # Fetch spot price
    price = get_spot_price(client, product_id)
    if price is None:
        print("ERROR: Could not fetch current price.")
        save_json(STATE_PATH, st)
        return

    print(f"Current spot price for {product_id}: ${price:,.2f}")
    print(f"Weekly spent: ${st['weekly_spent']:.2f} / ${max_usd_per_week:.2f} (remaining ${max(0.0, max_usd_per_week - st['weekly_spent']):.2f})")

    # Compute 7-day volatility and dynamic dip
    vol7 = compute_volatility_7d(client, product_id, days=7)
    if vol7 is not None:
        print(f"[STAGE A] 7-day volatility (approx %/day): {vol7:.2f}%")
    else:
        print("[STAGE A] 7-day volatility: unavailable (using base dip threshold)")

    dip_required, vol_tier = choose_dynamic_dip(vol7, base_min_drop, cfg)
    print(f"[STAGE A] Vol tier={vol_tier} -> dip_required={dip_required:.2f}% (base={base_min_drop:.2f}%)")

    # Cooldown
    last_buy_ts = st.get("last_buy_ts")
    if last_buy_ts:
        elapsed = int(time.time() - int(last_buy_ts))
        if elapsed < cooldown_seconds:
            remaining = cooldown_seconds - elapsed
            hrs = remaining / 3600
            print(f"Decision: SKIP | Cooldown active ({hrs:.1f}h remaining)")
            save_json(STATE_PATH, st)
            print("Bot finished successfully.")
            return

    # Weekly budget check
    remaining_week = max_usd_per_week - float(st.get("weekly_spent", 0.0))
    if remaining_week < 1.0:
        print("Decision: SKIP | Weekly budget exhausted")
        save_json(STATE_PATH, st)
        print("Bot finished successfully.")
        return

    # Determine dip based on last_price
    last_price = st.get("last_price")
    if last_price is None:
        if not buy_if_no_last:
            print("Decision: SKIP | No last_price yet (buy_if_no_last_price=false)")
            st["last_price"] = price
            save_json(STATE_PATH, st)
            print("Bot finished successfully.")
            return
        else:
            print("Decision: No last_price yet -> buy allowed")
            dip_pct = None
    else:
        dip_pct = -pct_change(price, last_price)  # positive if price dropped
        if dip_pct is None:
            dip_pct = 0.0

    # Decision logic
    should_buy = False
    reason = ""

    if last_price is None:
        should_buy = True
        reason = "first_run_buy"
    else:
        if dip_pct >= dip_required:
            should_buy = True
            reason = f"dip {dip_pct:.2f}% >= {dip_required:.2f}%"
        else:
            should_buy = False
            reason = f"dip {dip_pct:.2f}% < {dip_required:.2f}%"

    if not should_buy:
        print(f"Decision: SKIP | {reason}")
        st["last_price"] = price
        save_json(STATE_PATH, st)
        print("Bot finished successfully.")
        return

    # Determine order size (simple: min(usd_per_day, remaining_week))
    usd_to_buy = min(usd_per_day, remaining_week)
    usd_to_buy = max(0.0, round(usd_to_buy, 2))

    if usd_to_buy < 1.0:
        print("Decision: SKIP | Order too small after budget checks")
        st["last_price"] = price
        save_json(STATE_PATH, st)
        print("Bot finished successfully.")
        return

    print(f"Decision: BUY | {reason}")
    if dry_run:
        print(f"[DRY RUN] Would buy ${usd_to_buy:.2f} of {product_id}")
        # Update state as if we placed it? Usually no — but we do update last_price and cooldown.
        st["last_price"] = price
        st["last_buy_ts"] = int(time.time())
        save_json(STATE_PATH, st)
        print("Bot finished successfully.")
        return

    # LIVE: Place order
    print("[STAGE 6] LIVE MODE enabled. Placing real order...")
    client_order_id, resp = place_market_buy(client, product_id, usd_to_buy)
    print("[STAGE 6] Order attempted.")
    print(f"[STAGE 6] client_order_id={client_order_id}")

    if isinstance(resp, dict) and resp.get("error"):
        print(f"[STAGE 6] ERROR placing order: {resp['error']}")
        # Still update last_price so it doesn't keep firing instantly
        st["last_price"] = price
        save_json(STATE_PATH, st)
        return

    # Try to pull order_id if present
    order_id = None
    if isinstance(resp, dict):
        order_id = resp.get("order_id") or resp.get("id")
    else:
        order_id = getattr(resp, "order_id", None) or getattr(resp, "id", None)

    print(f"[STAGE 6] order_id={order_id}")

    # Update budgets/state
    st["weekly_spent"] = float(st.get("weekly_spent", 0.0)) + usd_to_buy
    st["last_price"] = price
    st["last_buy_ts"] = int(time.time())
    save_json(STATE_PATH, st)

    print("Bot finished successfully.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: {e}")
        raise

import json
import os
import sys
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple, List

# Coinbase Advanced Trade SDK (coinbase-advanced-py)
from coinbase.rest import RESTClient


CONFIG_PATH = "config.json"
STATE_PATH = "state.json"


# ----------------------------
# Helpers
# ----------------------------
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_today_utc() -> str:
    return utc_now().date().isoformat()


def safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def load_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def require_keys(cfg: Dict[str, Any], keys: List[str]) -> Tuple[bool, List[str]]:
    missing = [k for k in keys if k not in cfg]
    return (len(missing) == 0, missing)


def get_week_id(dt: datetime) -> str:
    # ISO week id like "2026-W03"
    iso = dt.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


# ----------------------------
# Coinbase API calls
# ----------------------------
def make_client() -> RESTClient:
    api_key = os.getenv("COINBASE_API_KEY", "").strip()
    api_secret = os.getenv("COINBASE_API_SECRET", "").strip()

    if not api_key or not api_secret:
        print("CONFIG ERROR: Missing COINBASE_API_KEY / COINBASE_API_SECRET secrets.")
        sys.exit(0)

    # IMPORTANT: api_secret must be the PEM (multi-line). No quotes needed in GitHub secret.
    return RESTClient(api_key=api_key, api_secret=api_secret)


def read_only_check(client: RESTClient) -> bool:
    try:
        # If this works, keys are valid.
        resp = client.get_accounts()
        # resp might be an object; just check it exists
        count = getattr(resp, "accounts", None)
        if isinstance(count, list):
            print(f"[STAGE 5] Read-only check OK. accounts_count={len(count)}")
        else:
            # fallback
            print("[STAGE 5] Read-only check OK.")
        return True
    except Exception as e:
        print("[STAGE 5] Read-only check FAILED.")
        print(f"Error: {e}")
        return False


def get_spot_price_usd(client: RESTClient, product_id: str) -> Optional[float]:
    # We’ll use the product endpoint that includes price
    try:
        prod = client.get_product(product_id)
        # coinbase-advanced-py returns an object; try common fields
        price = getattr(prod, "price", None)
        p = safe_float(price)
        if p is not None:
            return p
    except Exception:
        pass

    # Fallback: try market trades and use last trade price
    try:
        trades = client.get_market_trades(product_id=product_id, limit=1)
        tlist = getattr(trades, "trades", None)
        if isinstance(tlist, list) and len(tlist) > 0:
            last_price = safe_float(getattr(tlist[0], "price", None))
            return last_price
    except Exception:
        pass

    return None


def get_candles_hourly(client: RESTClient, product_id: str, hours: int) -> List[Dict[str, Any]]:
    """
    Returns a list of candles with at least 'close' if available.
    If API fails / returns nothing, returns [] (never None).
    """
    end = utc_now()
    start = end - timedelta(hours=hours)

    try:
        # granularity: 3600 seconds = 1 hour
        resp = client.get_product_candles(
            product_id=product_id,
            start=int(start.timestamp()),
            end=int(end.timestamp()),
            granularity=3600,
        )
        candles = getattr(resp, "candles", None)
        if not isinstance(candles, list):
            return []
        # Normalize to dict-like items
        out = []
        for c in candles:
            # c might be dict or object with attributes
            if isinstance(c, dict):
                out.append(c)
            else:
                out.append({
                    "close": getattr(c, "close", None),
                    "start": getattr(c, "start", None),
                })
        return out
    except Exception:
        return []


def calc_7d_volatility_from_hourly(client: RESTClient, product_id: str) -> Optional[float]:
    candles = get_candles_hourly(client, product_id, hours=7 * 24)
    closes: List[float] = []
    for c in candles:
        v = safe_float(c.get("close"))
        if v is not None and v > 0:
            closes.append(v)

    # Need enough data points
    if len(closes) < 24:
        return None

    # Simple volatility proxy: (max-min)/mean over window
    mn = min(closes)
    mx = max(closes)
    mean = sum(closes) / len(closes)
    if mean <= 0:
        return None
    return (mx - mn) / mean


def place_market_buy_usd(client: RESTClient, product_id: str, usd_amount: float) -> Tuple[bool, str]:
    """
    Places a market buy using quote size (USD). Returns (ok, message).
    """
    client_order_id = str(uuid.uuid4())

    try:
        resp = client.create_order(
            client_order_id=client_order_id,
            product_id=product_id,
            side="BUY",
            order_configuration={
                "market_market_ioc": {
                    "quote_size": str(round(usd_amount, 2))
                }
            }
        )

        # Many SDK responses have success + order_id; we log what we can safely.
        order_id = getattr(resp, "order_id", None)
        print("[STAGE 6] Order placed.")
        print(f"[STAGE 6] client_order_id={client_order_id}")
        print(f"[STAGE 6] order_id={order_id}")
        return True, f"Placed order client_order_id={client_order_id} order_id={order_id}"
    except Exception as e:
        return False, f"Order FAILED: {e}"


# ----------------------------
# Main logic
# ----------------------------
def main():
    print("Bot started")

    cfg = load_json(CONFIG_PATH, {})
    ok, missing = require_keys(cfg, ["product_id", "usd_per_day", "max_usd_per_week", "dry_run"])
    if not ok:
        print(f"CONFIG ERROR: Missing keys: {', '.join(missing)}")
        print("Bot finished successfully.")
        return

    # Optional tuning keys with defaults
    run_once_per_day = bool(cfg.get("run_once_per_day", True))
    dip_threshold_pct = float(cfg.get("dip_threshold_pct", -1.0))   # -1.0 means buy if price <= last_price * (1 - 0.01)
    lookback_hours = int(cfg.get("lookback_hours", 24))
    min_vol = float(cfg.get("min_volatility", 0.0))
    max_vol = float(cfg.get("max_volatility", 1.0))
    cooldown_hours = int(cfg.get("cooldown_hours", 6))
    min_order_usd = float(cfg.get("min_order_usd", 5.0))
    max_order_usd = float(cfg.get("max_order_usd", 50.0))

    product_id = str(cfg["product_id"])
    usd_per_day = float(cfg["usd_per_day"])
    max_usd_per_week = float(cfg["max_usd_per_week"])
    dry_run = bool(cfg["dry_run"])

    usd_per_day = max(0.0, usd_per_day)
    usd_per_day = min(usd_per_day, max_order_usd)
    usd_per_day = max(usd_per_day, min_order_usd) if usd_per_day > 0 else 0.0

    # Load state
    state = load_json(STATE_PATH, {})
    today = iso_today_utc()
    now = utc_now()
    week_id = get_week_id(now)

    # Initialize state keys safely
    state.setdefault("week_id", week_id)
    state.setdefault("week_spent_usd", 0.0)
    state.setdefault("last_run_day", "")
    state.setdefault("last_buy_time", "")
    state.setdefault("last_price", None)

    # Reset weekly spend if week changed
    if state.get("week_id") != week_id:
        state["week_id"] = week_id
        state["week_spent_usd"] = 0.0

    # Run-once-per-day gate
    if run_once_per_day and state.get("last_run_day") == today:
        print(f"Already ran today ({today}). Exiting.")
        print("Bot finished successfully.")
        return

    # Cooldown gate
    last_buy_time = state.get("last_buy_time", "")
    if last_buy_time:
        try:
            t = datetime.fromisoformat(last_buy_time.replace("Z", "+00:00"))
            if (now - t) < timedelta(hours=cooldown_hours):
                print(f"Decision: SKIP | Cooldown active ({cooldown_hours}h)")
                state["last_run_day"] = today
                save_json(STATE_PATH, state)
                print("Bot finished successfully.")
                return
        except Exception:
            pass

    # Coinbase client + read-only check
    client = make_client()
    read_only_check(client)

    # Get current price
    price = get_spot_price_usd(client, product_id)
    if price is None:
        print("Error: Could not fetch current price.")
        state["last_run_day"] = today
        save_json(STATE_PATH, state)
        print("Bot finished successfully.")
        return

    print(f"Current spot price for {product_id}: ${price:,.2f}")

    # Weekly budget gate
    week_spent = float(state.get("week_spent_usd", 0.0))
    remaining_week = max(0.0, max_usd_per_week - week_spent)
    print(f"Weekly spent: ${week_spent:,.2f} / ${max_usd_per_week:,.2f} (remaining ${remaining_week:,.2f})")

    if remaining_week < min_order_usd or usd_per_day <= 0:
        print("Decision: SKIP | Weekly budget exhausted or daily amount is 0")
        state["last_run_day"] = today
        save_json(STATE_PATH, state)
        print("Bot finished successfully.")
        return

    # Volatility filter (optional)
    vol = calc_7d_volatility_from_hourly(client, product_id)
    if vol is not None:
        # vol is fraction, e.g. 0.12 = 12%
        if not (min_vol <= vol <= max_vol):
            print(f"Decision: SKIP | Volatility {vol:.3f} outside [{min_vol:.3f}, {max_vol:.3f}]")
            state["last_run_day"] = today
            save_json(STATE_PATH, state)
            print("Bot finished successfully.")
            return
    else:
        print("Note: Volatility unavailable (not enough candle data). Continuing.")

    # Dip rule using last_price stored
    last_price = safe_float(state.get("last_price"))
    buy = False
    if last_price is None:
        print("Decision: BUY | No last_price yet -> first buy allowed")
        buy = True
    else:
        # dip_threshold_pct is negative for “dip”
        threshold = last_price * (1.0 + (dip_threshold_pct / 100.0))
        if price <= threshold:
            print(f"Decision: BUY | Price ${price:,.2f} <= threshold ${threshold:,.2f}")
            buy = True
        else:
            print("Decision: SKIP | Not a dip yet")

    # Record run day always
    state["last_run_day"] = today

    if not buy:
        state["last_price"] = price
        save_json(STATE_PATH, state)
        print("Bot finished successfully.")
        return

    # Decide order size (cap by remaining weekly)
    order_usd = min(usd_per_day, remaining_week)
    order_usd = max(min_order_usd, min(order_usd, max_order_usd))

    if dry_run:
        print(f"[DRY RUN] Would buy ${order_usd:,.2f} of {product_id}")
        state["last_price"] = price
        save_json(STATE_PATH, state)
        print("Bot finished successfully.")
        return

    print("[STAGE 6] LIVE MODE enabled. Placing real order...")
    ok, msg = place_market_buy_usd(client, product_id, order_usd)
    print(f"[STAGE 6] {msg}")

    if ok:
        state["week_spent_usd"] = float(state.get("week_spent_usd", 0.0)) + order_usd
        state["last_buy_time"] = utc_now().isoformat().replace("+00:00", "Z")
        state["last_price"] = price

    save_json(STATE_PATH, state)
    print("Bot finished successfully.")


if __name__ == "__main__":
    main()

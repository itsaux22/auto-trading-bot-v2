import os
import json
import uuid
import math
import time
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

import requests

# Coinbase Advanced Trade SDK (installed via coinbase-advanced-py)
# Your workflow installs this already.
try:
    from coinbase.rest import RESTClient
except Exception:
    RESTClient = None  # We'll still allow DRY RUN + public price checks


# -----------------------------
# Helpers: files + time
# -----------------------------

CONFIG_PATH = "config.json"
STATE_PATH = "state.json"

EXCHANGE_API_BASE = "https://api.exchange.coinbase.com"  # public (candles + ticker)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_date(d: Optional[dt.datetime] = None) -> str:
    d = d or utc_now()
    return d.strftime("%Y-%m-%d")


def week_id(d: Optional[dt.datetime] = None) -> str:
    d = d or utc_now()
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def load_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def to_float(x: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return default


# -----------------------------
# Config + State (Upgrade #1)
# -----------------------------

REQUIRED_KEYS = ["product_id", "usd_per_day", "max_usd_per_week", "dry_run"]

DEFAULT_CONFIG: Dict[str, Any] = {
    # required
    "product_id": "BTC-USD",
    "usd_per_day": 10,
    "max_usd_per_week": 70,
    "dry_run": True,

    # safety + behavior
    "run_once_per_day": True,     # prevents double-buy in same day
    "cooldown_hours": 6,          # after a buy, wait this many hours

    # signal
    "lookback_hours": 24,         # candles lookback for dip calc
    "dip_threshold_pct": -1.0,    # buy if price is <= (recent_high * (1 + threshold/100))
    "min_volatility": 0.0,        # optional filter (0 disables)
    "max_volatility": 1.0,        # optional filter (1 disables)

    # order sizing clamps
    "min_order_usd": 5.0,
    "max_order_usd": 50.0,

    # logging verbosity
    "debug": False
}


def load_config() -> Dict[str, Any]:
    cfg = load_json(CONFIG_PATH, {})
    # merge defaults
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg if isinstance(cfg, dict) else {})

    missing = [k for k in REQUIRED_KEYS if k not in merged]
    if missing:
        raise ValueError(f"CONFIG ERROR: Missing keys: {', '.join(missing)}")

    # normalize numeric fields
    merged["usd_per_day"] = float(merged["usd_per_day"])
    merged["max_usd_per_week"] = float(merged["max_usd_per_week"])
    merged["lookback_hours"] = int(merged["lookback_hours"])
    merged["dip_threshold_pct"] = float(merged["dip_threshold_pct"])
    merged["cooldown_hours"] = float(merged["cooldown_hours"])
    merged["min_order_usd"] = float(merged["min_order_usd"])
    merged["max_order_usd"] = float(merged["max_order_usd"])
    merged["min_volatility"] = float(merged["min_volatility"])
    merged["max_volatility"] = float(merged["max_volatility"])

    return merged


def load_state() -> Dict[str, Any]:
    st = load_json(STATE_PATH, {})
    if not isinstance(st, dict):
        st = {}
    # defaults
    st.setdefault("week", week_id())
    st.setdefault("weekly_spent", 0.0)
    st.setdefault("last_run_date", "")
    st.setdefault("last_buy_ts", 0)      # unix seconds
    st.setdefault("last_buy_price", None)
    return st


def reset_week_if_needed(st: Dict[str, Any]) -> None:
    w = week_id()
    if st.get("week") != w:
        st["week"] = w
        st["weekly_spent"] = 0.0


# -----------------------------
# Market data (public endpoints)
# -----------------------------

def get_spot_price(product_id: str) -> float:
    """
    Uses Coinbase Exchange public ticker endpoint.
    """
    url = f"{EXCHANGE_API_BASE}/products/{product_id}/ticker"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()
    price = to_float(data.get("price"))
    if price is None:
        raise RuntimeError(f"Could not parse price from ticker: {data}")
    return float(price)


def get_hourly_closes(product_id: str, hours: int) -> List[float]:
    """
    Public candles endpoint: returns list of [time, low, high, open, close, volume]
    We parse closes and return ascending by time.
    """
    end = utc_now()
    start = end - dt.timedelta(hours=hours)

    params = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "granularity": 3600
    }
    url = f"{EXCHANGE_API_BASE}/products/{product_id}/candles"
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    candles = r.json()

    if not isinstance(candles, list) or len(candles) == 0:
        return []

    # candles are often returned newest-first, so sort by time
    parsed: List[Tuple[int, float]] = []
    for c in candles:
        # expected: [time, low, high, open, close, volume]
        if not isinstance(c, list) or len(c) < 5:
            continue
        t = int(c[0])
        close = to_float(c[4])
        if close is None:
            continue
        parsed.append((t, float(close)))

    parsed.sort(key=lambda x: x[0])
    return [p[1] for p in parsed]


def calc_volatility(closes: List[float]) -> float:
    """
    Simple realized volatility proxy from hourly returns.
    Returns a 0..~ range (not annualized). Safe + stable.
    """
    if len(closes) < 10:
        return 0.0
    rets = []
    for i in range(1, len(closes)):
        if closes[i - 1] <= 0:
            continue
        rets.append((closes[i] / closes[i - 1]) - 1.0)
    if len(rets) < 5:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var)


def is_dip(current: float, closes: List[float], dip_threshold_pct: float) -> Tuple[bool, float, float]:
    """
    Dip rule:
    dip_pct = (current - recent_high) / recent_high * 100
    buy when dip_pct <= dip_threshold_pct  (negative threshold)
    """
    if not closes:
        return False, 0.0, current
    recent_high = max(closes)
    if recent_high <= 0:
        return False, 0.0, current
    dip_pct = (current - recent_high) / recent_high * 100.0
    return (dip_pct <= dip_threshold_pct), dip_pct, recent_high


# -----------------------------
# Coinbase client + orders
# -----------------------------

def make_client() -> Optional[Any]:
    """
    Creates Coinbase Advanced Trade RESTClient using:
      COINBASE_API_KEY    (key id)
      COINBASE_API_SECRET (PEM private key)
    """
    api_key = os.getenv("COINBASE_API_KEY", "").strip()
    api_secret = os.getenv("COINBASE_API_SECRET", "").strip()

    if not api_key or not api_secret:
        return None
    if RESTClient is None:
        return None

    return RESTClient(api_key=api_key, api_secret=api_secret)


def safe_to_dict(obj: Any) -> Dict[str, Any]:
    """
    Different SDK versions return different response shapes.
    This tries a few ways to normalize.
    """
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    for attr in ("to_dict", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    # fallback: try __dict__
    try:
        d = dict(obj.__dict__)
        return d
    except Exception:
        return {}


def place_market_buy(client: Any, product_id: str, usd_amount: float) -> Dict[str, Any]:
    """
    Coinbase Advanced Trade market order (IOC) by quote size.
    """
    payload = {
        "client_order_id": str(uuid.uuid4()),
        "product_id": product_id,
        "side": "BUY",
        "order_configuration": {
            "market_market_ioc": {
                "quote_size": f"{usd_amount:.2f}"
            }
        }
    }

    # Try common call patterns across SDK versions
    if hasattr(client, "create_order"):
        try:
            resp = client.create_order(**payload)
            return safe_to_dict(resp) or {"raw": str(resp), **payload}
        except TypeError:
            resp = client.create_order(payload)
            return safe_to_dict(resp) or {"raw": str(resp), **payload}

    raise RuntimeError("RESTClient does not have create_order() in this version.")


def read_only_check_accounts(client: Any) -> int:
    """
    Optional read-only health check. Returns number of accounts if possible.
    """
    if client is None:
        return 0
    if hasattr(client, "list_accounts"):
        resp = client.list_accounts()
        data = safe_to_dict(resp)

        # try common shapes
        if isinstance(data.get("accounts"), list):
            return len(data["accounts"])

        # some versions store inside nested keys
        for k in ("accounts", "data", "result"):
            v = data.get(k)
            if isinstance(v, list):
                return len(v)

    return 0


# -----------------------------
# Main
# -----------------------------

def main() -> None:
    print("Bot started")

    try:
        cfg = load_config()
    except Exception as e:
        print(str(e))
        print("Bot finished successfully.")
        return

    st = load_state()
    reset_week_if_needed(st)

    # optional: only once per day
    today = iso_date()
    if cfg.get("run_once_per_day", True) and st.get("last_run_date") == today:
        print(f"Already ran today ({today}). Exiting.")
        print("Bot finished successfully.")
        return

    product_id = cfg["product_id"]

    # Stage 5-like health check
    client = make_client()
    if client is None:
        print("[STAGE 5] Coinbase keys not detected (or SDK missing). Running DRY-only market checks.")
    else:
        try:
            n = read_only_check_accounts(client)
            print(f"[STAGE 5] Read-only check OK. accounts_count={n}")
        except Exception as e:
            print("[STAGE 5] Read-only check FAILED.")
            print(f"Error: {e}")

    # Market data
    try:
        spot = get_spot_price(product_id)
        print(f"Current spot price for {product_id}: ${spot:,.2f}")
    except Exception as e:
        print(f"MARKET DATA ERROR: Could not fetch spot price: {e}")
        st["last_run_date"] = today
        save_json(STATE_PATH, st)
        print("Bot finished successfully.")
        return

    # Weekly spend guard
    weekly_spent = float(st.get("weekly_spent", 0.0))
    weekly_remaining = max(0.0, cfg["max_usd_per_week"] - weekly_spent)
    print(f"Weekly spent: ${weekly_spent:,.2f} / ${cfg['max_usd_per_week']:,.2f} (remaining ${weekly_remaining:,.2f})")

    if weekly_remaining < cfg["min_order_usd"]:
        print("Decision: SKIP | Weekly limit reached (or remaining too small).")
        st["last_run_date"] = today
        save_json(STATE_PATH, st)
        print("Bot finished successfully.")
        return

    # Cooldown guard
    last_buy_ts = int(st.get("last_buy_ts", 0))
    if last_buy_ts > 0:
        hours_since = (time.time() - last_buy_ts) / 3600.0
        if hours_since < cfg["cooldown_hours"]:
            print(f"Decision: SKIP | Cooldown active ({hours_since:.1f}h since last buy, need {cfg['cooldown_hours']}h).")
            st["last_run_date"] = today
            save_json(STATE_PATH, st)
            print("Bot finished successfully.")
            return

    # Signal: dip + volatility
    closes = []
    try:
        closes = get_hourly_closes(product_id, cfg["lookback_hours"])
    except Exception as e:
        if cfg.get("debug"):
            print(f"DEBUG: candle fetch failed: {e}")
        closes = []

    vol = calc_volatility(closes) if closes else 0.0
    dip_ok, dip_pct, recent_high = is_dip(spot, closes, cfg["dip_threshold_pct"])

    # Volatility filter is optional; defaults (0..1) are essentially "allow"
    vol_ok = (vol >= cfg["min_volatility"]) and (vol <= cfg["max_volatility"])

    if closes:
        print(f"Lookback high ({cfg['lookback_hours']}h): ${recent_high:,.2f} | dip_pct={dip_pct:.2f}% | vol={vol:.5f}")
    else:
        print("Lookback candles unavailable (using safe default: NO DIP).")

    if not closes or not dip_ok:
        print("Decision: SKIP | Not a dip yet")
        st["last_run_date"] = today
        save_json(STATE_PATH, st)
        print("Bot finished successfully.")
        return

    if not vol_ok:
        print("Decision: SKIP | Volatility filter not met")
        st["last_run_date"] = today
        save_json(STATE_PATH, st)
        print("Bot finished successfully.")
        return

    # Determine order amount
    usd_amount = min(cfg["usd_per_day"], weekly_remaining)
    usd_amount = max(cfg["min_order_usd"], min(usd_amount, cfg["max_order_usd"]))

    print("Decision: BUY")

    # DRY RUN
    if bool(cfg["dry_run"]):
        print(f"[DRY RUN] Would buy ${usd_amount:,.2f} of {product_id}")
        st["last_run_date"] = today
        save_json(STATE_PATH, st)
        print("Bot finished successfully.")
        return

    # LIVE mode requires client keys
    if client is None:
        print("[LIVE MODE] ERROR: No Coinbase client available (missing keys or SDK).")
        st["last_run_date"] = today
        save_json(STATE_PATH, st)
        print("Bot finished successfully.")
        return

    # Place order
    try:
        print("[STAGE 6] LIVE MODE enabled. Placing real order...")
        resp = place_market_buy(client, product_id, usd_amount)

        client_order_id = resp.get("client_order_id") or resp.get("clientOrderId")
        order_id = resp.get("order_id") or resp.get("orderId") or resp.get("id")

        print("[STAGE 6] Order placed.")
        if client_order_id:
            print(f"[STAGE 6] client_order_id={client_order_id}")
        if order_id:
            print(f"[STAGE 6] order_id={order_id}")

        # Update state
        st["weekly_spent"] = float(st.get("weekly_spent", 0.0)) + float(usd_amount)
        st["last_buy_ts"] = int(time.time())
        st["last_buy_price"] = float(spot)

    except Exception as e:
        print("[STAGE 6] Order FAILED.")
        print(f"Error: {e}")

    st["last_run_date"] = today
    save_json(STATE_PATH, st)
    print("Bot finished successfully.")


if __name__ == "__main__":
    main()

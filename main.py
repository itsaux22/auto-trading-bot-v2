import os
import json
import uuid
import math
import time
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    from coinbase.rest import RESTClient
except Exception:
    RESTClient = None

CONFIG_PATH = "config.json"
STATE_PATH = "state.json"
EXCHANGE_API_BASE = "https://api.exchange.coinbase.com"


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


REQUIRED_KEYS = ["product_id", "usd_per_day", "max_usd_per_week", "dry_run"]

DEFAULT_CONFIG: Dict[str, Any] = {
    "product_id": "BTC-USD",
    "usd_per_day": 10,
    "max_usd_per_week": 70,
    "dry_run": True,

    "run_once_per_day": True,
    "cooldown_hours": 6,

    "lookback_hours": 24,
    "dip_threshold_pct": -1.0,

    "trend_filter_enabled": True,
    "trend_sma_hours": 72,
    "trend_min_pct_above_sma": -0.5,

    "kill_switch_enabled": True,
    "kill_switch_lookback_hours": 168,
    "kill_switch_drawdown_pct": -10.0,

    "dip_scaling_enabled": True,
    "dip_scaling_max_mult": 2.0,
    "dip_scaling_full_mult_at_pct": -5.0,

    "min_order_usd": 5.0,
    "max_order_usd": 50.0,

    # ✅ Upgrade #2 risk controls
    "pause_trading": False,
    "max_buys_per_day": 1,
    "trade_days_utc": [0, 1, 2, 3, 4, 5, 6],

    "debug": False
}


def load_config() -> Dict[str, Any]:
    cfg = load_json(CONFIG_PATH, {})
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg if isinstance(cfg, dict) else {})

    missing = [k for k in REQUIRED_KEYS if k not in merged]
    if missing:
        raise ValueError(f"CONFIG ERROR: Missing keys: {', '.join(missing)}")

    merged["usd_per_day"] = float(merged["usd_per_day"])
    merged["max_usd_per_week"] = float(merged["max_usd_per_week"])
    merged["lookback_hours"] = int(merged["lookback_hours"])
    merged["dip_threshold_pct"] = float(merged["dip_threshold_pct"])
    merged["cooldown_hours"] = float(merged["cooldown_hours"])

    merged["trend_sma_hours"] = int(merged["trend_sma_hours"])
    merged["trend_min_pct_above_sma"] = float(merged["trend_min_pct_above_sma"])
    merged["kill_switch_lookback_hours"] = int(merged["kill_switch_lookback_hours"])
    merged["kill_switch_drawdown_pct"] = float(merged["kill_switch_drawdown_pct"])

    merged["dip_scaling_max_mult"] = float(merged["dip_scaling_max_mult"])
    merged["dip_scaling_full_mult_at_pct"] = float(merged["dip_scaling_full_mult_at_pct"])

    merged["min_order_usd"] = float(merged["min_order_usd"])
    merged["max_order_usd"] = float(merged["max_order_usd"])

    merged["pause_trading"] = bool(merged.get("pause_trading", False))
    merged["max_buys_per_day"] = int(merged.get("max_buys_per_day", 1))
    merged["trade_days_utc"] = merged.get("trade_days_utc", [0,1,2,3,4,5,6])

    return merged


def load_state() -> Dict[str, Any]:
    st = load_json(STATE_PATH, {})
    if not isinstance(st, dict):
        st = {}
    st.setdefault("week", week_id())
    st.setdefault("weekly_spent", 0.0)
    st.setdefault("last_run_date", "")
    st.setdefault("last_buy_ts", 0)
    st.setdefault("last_buy_price", None)

    # ✅ Upgrade #2 state
    st.setdefault("buys_by_day", {})  # {"YYYY-MM-DD": int}
    return st


def reset_week_if_needed(st: Dict[str, Any]) -> None:
    w = week_id()
    if st.get("week") != w:
        st["week"] = w
        st["weekly_spent"] = 0.0


def get_spot_price(product_id: str) -> float:
    url = f"{EXCHANGE_API_BASE}/products/{product_id}/ticker"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()
    price = to_float(data.get("price"))
    if price is None:
        raise RuntimeError(f"Could not parse price from ticker: {data}")
    return float(price)


def get_hourly_closes(product_id: str, hours: int) -> List[float]:
    end = utc_now()
    start = end - dt.timedelta(hours=hours)
    params = {"start": start.isoformat(), "end": end.isoformat(), "granularity": 3600}
    url = f"{EXCHANGE_API_BASE}/products/{product_id}/candles"
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    candles = r.json()

    if not isinstance(candles, list) or len(candles) == 0:
        return []

    parsed: List[Tuple[int, float]] = []
    for c in candles:
        if not isinstance(c, list) or len(c) < 5:
            continue
        t = int(c[0])
        close = to_float(c[4])
        if close is None:
            continue
        parsed.append((t, float(close)))

    parsed.sort(key=lambda x: x[0])
    return [p[1] for p in parsed]


def sma(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def is_dip(current: float, closes: List[float], dip_threshold_pct: float) -> Tuple[bool, float, float]:
    if not closes:
        return False, 0.0, current
    recent_high = max(closes)
    if recent_high <= 0:
        return False, 0.0, current
    dip_pct = (current - recent_high) / recent_high * 100.0
    return (dip_pct <= dip_threshold_pct), dip_pct, recent_high


def pct_from(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return (a - b) / b * 100.0


def trend_ok(current: float, trend_closes: List[float], min_pct_above_sma: float) -> Tuple[bool, Optional[float], float]:
    s = sma(trend_closes)
    if s is None:
        return True, None, 0.0
    pct_vs = pct_from(current, s)
    return (pct_vs >= min_pct_above_sma), s, pct_vs


def kill_switch_ok(current: float, closes_7d: List[float], drawdown_threshold_pct: float) -> Tuple[bool, Optional[float], Optional[float]]:
    if not closes_7d:
        return True, None, None
    high = max(closes_7d)
    if high <= 0:
        return True, None, None
    dd_pct = (current - high) / high * 100.0
    return (dd_pct > drawdown_threshold_pct), high, dd_pct


def dip_scaled_amount(base_usd: float, dip_pct: float, full_mult_at_pct: float, max_mult: float) -> float:
    if dip_pct >= 0:
        return base_usd
    denom = abs(full_mult_at_pct) if full_mult_at_pct != 0 else 1.0
    t = min(1.0, abs(dip_pct) / denom)
    mult = 1.0 + (max_mult - 1.0) * t
    return base_usd * mult


def make_client() -> Optional[Any]:
    api_key = os.getenv("COINBASE_API_KEY", "").strip()
    api_secret = os.getenv("COINBASE_API_SECRET", "").strip()
    if not api_key or not api_secret or RESTClient is None:
        return None
    return RESTClient(api_key=api_key, api_secret=api_secret)


def safe_to_dict(obj: Any) -> Dict[str, Any]:
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
    try:
        return dict(obj.__dict__)
    except Exception:
        return {}


def place_market_buy(client: Any, product_id: str, usd_amount: float) -> Dict[str, Any]:
    payload = {
        "client_order_id": str(uuid.uuid4()),
        "product_id": product_id,
        "side": "BUY",
        "order_configuration": {"market_market_ioc": {"quote_size": f"{usd_amount:.2f}"}}
    }
    try:
        resp = client.create_order(**payload)
    except TypeError:
        resp = client.create_order(payload)
    return safe_to_dict(resp) or {"raw": str(resp), **payload}


def read_only_check_accounts(client: Any) -> int:
    if client is None or not hasattr(client, "list_accounts"):
        return 0
    resp = client.list_accounts()
    data = safe_to_dict(resp)
    if isinstance(data.get("accounts"), list):
        return len(data["accounts"])
    for k in ("accounts", "data", "result"):
        v = data.get(k)
        if isinstance(v, list):
            return len(v)
    return 0


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

    today = iso_date()

    # ✅ Upgrade #2: Pause switch
    if cfg.get("pause_trading", False):
        print("Decision: SKIP | pause_trading=true")
        st["last_run_date"] = today
        save_json(STATE_PATH, st)
        print("Bot finished successfully.")
        return

    # ✅ Upgrade #2: allowed days
    weekday = utc_now().weekday()  # Mon=0..Sun=6
    allowed_days = cfg.get("trade_days_utc", [0,1,2,3,4,5,6])
    if weekday not in allowed_days:
        print(f"Decision: SKIP | Not an allowed trade day (weekday={weekday})")
        st["last_run_date"] = today
        save_json(STATE_PATH, st)
        print("Bot finished successfully.")
        return

    # ✅ Upgrade #2: max buys per day
    buys_by_day = st.get("buys_by_day", {})
    if not isinstance(buys_by_day, dict):
        buys_by_day = {}
    buys_today = int(buys_by_day.get(today, 0))
    if buys_today >= int(cfg.get("max_buys_per_day", 1)):
        print(f"Decision: SKIP | Max buys per day reached ({buys_today}/{cfg.get('max_buys_per_day')})")
        st["last_run_date"] = today
        st["buys_by_day"] = buys_by_day
        save_json(STATE_PATH, st)
        print("Bot finished successfully.")
        return

    # run once per day (optional)
    if cfg.get("run_once_per_day", True) and st.get("last_run_date") == today:
        print(f"Already ran today ({today}). Exiting.")
        print("Bot finished successfully.")
        return

    product_id = cfg["product_id"]

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

    # Spot
    try:
        spot = get_spot_price(product_id)
        print(f"Current spot price for {product_id}: ${spot:,.2f}")
    except Exception as e:
        print(f"MARKET DATA ERROR: Could not fetch spot price: {e}")
        st["last_run_date"] = today
        st["buys_by_day"] = buys_by_day
        save_json(STATE_PATH, st)
        print("Bot finished successfully.")
        return

    # Weekly cap
    weekly_spent = float(st.get("weekly_spent", 0.0))
    weekly_remaining = max(0.0, cfg["max_usd_per_week"] - weekly_spent)
    print(f"Weekly spent: ${weekly_spent:,.2f} / ${cfg['max_usd_per_week']:,.2f} (remaining ${weekly_remaining:,.2f})")

    if weekly_remaining < cfg["min_order_usd"]:
        print("Decision: SKIP | Weekly limit reached (or remaining too small).")
        st["last_run_date"] = today
        st["buys_by_day"] = buys_by_day
        save_json(STATE_PATH, st)
        print("Bot finished successfully.")
        return

    # Cooldown
    last_buy_ts = int(st.get("last_buy_ts", 0))
    if last_buy_ts > 0:
        hours_since = (time.time() - last_buy_ts) / 3600.0
        if hours_since < cfg["cooldown_hours"]:
            print(f"Decision: SKIP | Cooldown active ({hours_since:.1f}h since last buy, need {cfg['cooldown_hours']}h).")
            st["last_run_date"] = today
            st["buys_by_day"] = buys_by_day
            save_json(STATE_PATH, st)
            print("Bot finished successfully.")
            return

    # Dip check
    try:
        dip_closes = get_hourly_closes(product_id, cfg["lookback_hours"])
    except Exception as e:
        if cfg.get("debug"):
            print(f"DEBUG: dip candle fetch failed: {e}")
        dip_closes = []

    dip_ok, dip_pct, recent_high = is_dip(spot, dip_closes, cfg["dip_threshold_pct"])
    if dip_closes:
        print(f"Lookback high ({cfg['lookback_hours']}h): ${recent_high:,.2f} | dip_pct={dip_pct:.2f}% (threshold {cfg['dip_threshold_pct']}%)")
    else:
        print("Lookback candles unavailable (safe default: NO DIP).")
        dip_ok = False

    if not dip_ok:
        print("Decision: SKIP | Not a dip yet")
        st["last_run_date"] = today
        st["buys_by_day"] = buys_by_day
        save_json(STATE_PATH, st)
        print("Bot finished successfully.")
        return

    # Trend filter
    if cfg.get("trend_filter_enabled", True):
        try:
            trend_closes = get_hourly_closes(product_id, cfg["trend_sma_hours"])
        except Exception as e:
            if cfg.get("debug"):
                print(f"DEBUG: trend candle fetch failed: {e}")
            trend_closes = []

        ok, s, pct_vs = trend_ok(spot, trend_closes, cfg["trend_min_pct_above_sma"])
        if s is not None:
            print(f"Trend SMA({cfg['trend_sma_hours']}h): ${s:,.2f} | price_vs_sma={pct_vs:.2f}% (min {cfg['trend_min_pct_above_sma']}%)")
        if not ok:
            print("Decision: SKIP | Trend filter says downtrend")
            st["last_run_date"] = today
            st["buys_by_day"] = buys_by_day
            save_json(STATE_PATH, st)
            print("Bot finished successfully.")
            return

    # Kill switch
    if cfg.get("kill_switch_enabled", True):
        try:
            closes_7d = get_hourly_closes(product_id, cfg["kill_switch_lookback_hours"])
        except Exception as e:
            if cfg.get("debug"):
                print(f"DEBUG: 7d candle fetch failed: {e}")
            closes_7d = []

        ok, high7, dd_pct = kill_switch_ok(spot, closes_7d, cfg["kill_switch_drawdown_pct"])
        if high7 is not None and dd_pct is not None:
            print(f"Kill-switch 7d high: ${high7:,.2f} | drawdown={dd_pct:.2f}% (threshold {cfg['kill_switch_drawdown_pct']}%)")
        if not ok:
            print("Decision: SKIP | Kill-switch triggered")
            st["last_run_date"] = today
            st["buys_by_day"] = buys_by_day
            save_json(STATE_PATH, st)
            print("Bot finished successfully.")
            return

    # Dip scaling
    usd_amount = cfg["usd_per_day"]
    if cfg.get("dip_scaling_enabled", True):
        usd_amount = dip_scaled_amount(
            base_usd=cfg["usd_per_day"],
            dip_pct=dip_pct,
            full_mult_at_pct=cfg["dip_scaling_full_mult_at_pct"],
            max_mult=cfg["dip_scaling_max_mult"],
        )

    usd_amount = min(float(usd_amount), weekly_remaining)
    usd_amount = max(cfg["min_order_usd"], min(usd_amount, cfg["max_order_usd"]))

    print("Decision: BUY")

    # DRY RUN
    if bool(cfg["dry_run"]):
        print(f"[DRY RUN] Would buy ${usd_amount:,.2f} of {product_id}")
        st["last_run_date"] = today
        st["buys_by_day"] = buys_by_day
        save_json(STATE_PATH, st)
        print("Bot finished successfully.")
        return

    if client is None:
        print("[LIVE MODE] ERROR: No Coinbase client available.")
        st["last_run_date"] = today
        st["buys_by_day"] = buys_by_day
        save_json(STATE_PATH, st)
        print("Bot finished successfully.")
        return

    # Place order
    try:
        print("[STAGE 6] LIVE MODE enabled. Placing real order...")
        resp = place_market_buy(client, product_id, usd_amount)

        print("[STAGE 6] Order placed.")
        st["weekly_spent"] = float(st.get("weekly_spent", 0.0)) + float(usd_amount)
        st["last_buy_ts"] = int(time.time())
        st["last_buy_price"] = float(spot)

        # ✅ increment buys today
        buys_by_day[today] = buys_today + 1
        st["buys_by_day"] = buys_by_day

    except Exception as e:
        print("[STAGE 6] Order FAILED.")
        print(f"Error: {e}")

    st["last_run_date"] = today
    save_json(STATE_PATH, st)
    print("Bot finished successfully.")


if __name__ == "__main__":
    main()

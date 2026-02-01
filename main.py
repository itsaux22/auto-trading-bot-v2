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
LOG_PATH = "trades_log.jsonl"
EXCHANGE_API_BASE = "https://api.exchange.coinbase.com"


# -----------------------------
# Time + IO helpers
# -----------------------------
def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_date(d: Optional[dt.datetime] = None) -> str:
    d = d or utc_now()
    return d.strftime("%Y-%m-%d")


def iso_ts(d: Optional[dt.datetime] = None) -> str:
    d = d or utc_now()
    return d.isoformat().replace("+00:00", "Z")


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


def append_jsonl(path: str, obj: Dict[str, Any]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n")


def to_float(x: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return default


# -----------------------------
# Config + State
# -----------------------------
REQUIRED_KEYS = ["product_id", "usd_per_day", "max_usd_per_week", "dry_run"]

DEFAULT_CONFIG: Dict[str, Any] = {
    # required
    "product_id": "BTC-USD",
    "usd_per_day": 10,
    "max_usd_per_week": 70,
    "dry_run": True,

    # safety
    "run_once_per_day": True,
    "cooldown_hours": 12,
    "pause_trading": False,
    "max_buys_per_day": 1,
    "trade_days_utc": [0, 1, 2, 3, 4, 5, 6],  # Mon..Sun

    # dip + guards
    "lookback_hours": 24,
    "dip_threshold_pct": -1.0,

    "trend_filter_enabled": True,
    "trend_sma_hours": 72,
    "trend_min_pct_above_sma": -0.5,

    "kill_switch_enabled": True,
    "kill_switch_lookback_hours": 168,
    "kill_switch_drawdown_pct": -12.0,

    "dip_scaling_enabled": True,
    "dip_scaling_max_mult": 1.8,
    "dip_scaling_full_mult_at_pct": -6.0,

    # order clamps
    "min_order_usd": 5.0,
    "max_order_usd": 25.0,

    # AI scoring
    "ai_enabled": True,
    "ai_score_threshold": 70,
    "ai_debug_print": True,

    "rsi_period": 14,
    "rsi_oversold": 30,
    "rsi_very_oversold": 25,

    "mom_fast_hours": 6,
    "mom_slow_hours": 24,
    "history_hours": 200,

    # logging
    "log_each_run": True,

    # notifications (Upgrade 5 will use these; safe to keep now)
    "notify_on_buy": True,
    "notify_on_error": True,

    "debug": False
}


def load_config() -> Dict[str, Any]:
    cfg = load_json(CONFIG_PATH, {})
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg if isinstance(cfg, dict) else {})

    missing = [k for k in REQUIRED_KEYS if k not in merged]
    if missing:
        raise ValueError(f"CONFIG ERROR: Missing keys: {', '.join(missing)}")

    # normalize numbers
    merged["usd_per_day"] = float(merged["usd_per_day"])
    merged["max_usd_per_week"] = float(merged["max_usd_per_week"])
    merged["cooldown_hours"] = float(merged["cooldown_hours"])
    merged["lookback_hours"] = int(merged["lookback_hours"])
    merged["dip_threshold_pct"] = float(merged["dip_threshold_pct"])

    merged["trend_sma_hours"] = int(merged["trend_sma_hours"])
    merged["trend_min_pct_above_sma"] = float(merged["trend_min_pct_above_sma"])

    merged["kill_switch_lookback_hours"] = int(merged["kill_switch_lookback_hours"])
    merged["kill_switch_drawdown_pct"] = float(merged["kill_switch_drawdown_pct"])

    merged["dip_scaling_max_mult"] = float(merged["dip_scaling_max_mult"])
    merged["dip_scaling_full_mult_at_pct"] = float(merged["dip_scaling_full_mult_at_pct"])

    merged["min_order_usd"] = float(merged["min_order_usd"])
    merged["max_order_usd"] = float(merged["max_order_usd"])

    merged["max_buys_per_day"] = int(merged["max_buys_per_day"])
    merged["ai_score_threshold"] = int(merged["ai_score_threshold"])

    merged["rsi_period"] = int(merged["rsi_period"])
    merged["rsi_oversold"] = int(merged["rsi_oversold"])
    merged["rsi_very_oversold"] = int(merged["rsi_very_oversold"])

    merged["mom_fast_hours"] = int(merged["mom_fast_hours"])
    merged["mom_slow_hours"] = int(merged["mom_slow_hours"])
    merged["history_hours"] = int(merged["history_hours"])

    merged["pause_trading"] = bool(merged.get("pause_trading", False))
    merged["log_each_run"] = bool(merged.get("log_each_run", True))
    merged["notify_on_buy"] = bool(merged.get("notify_on_buy", True))
    merged["notify_on_error"] = bool(merged.get("notify_on_error", True))

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
    st.setdefault("buys_by_day", {})  # {"YYYY-MM-DD": int}

    # ✅ Upgrade 4: estimated holdings tracking
    st.setdefault("est_total_usd_spent", 0.0)
    st.setdefault("est_total_base_qty", 0.0)     # e.g., BTC amount estimated
    st.setdefault("est_avg_entry", None)

    return st


def reset_week_if_needed(st: Dict[str, Any]) -> None:
    w = week_id()
    if st.get("week") != w:
        st["week"] = w
        st["weekly_spent"] = 0.0


# -----------------------------
# Market data (public)
# -----------------------------
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


# -----------------------------
# Indicators
# -----------------------------
def sma(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def pct_from(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return (a - b) / b * 100.0


def rsi(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None

    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses += -delta

    avg_gain = gains / period
    avg_loss = losses / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def dip_stats(current: float, closes: List[float]) -> Tuple[Optional[float], Optional[float]]:
    if not closes:
        return None, None
    high = max(closes)
    if high <= 0:
        return None, None
    dip_pct = (current - high) / high * 100.0
    return high, dip_pct


def momentum_pct(closes: List[float], hours: int) -> Optional[float]:
    if len(closes) < hours + 1:
        return None
    now = closes[-1]
    past = closes[-1 - hours]
    if past <= 0:
        return None
    return (now - past) / past * 100.0


# -----------------------------
# AI scoring
# -----------------------------
def compute_ai_score(spot: float, closes: List[float], cfg: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    details: Dict[str, Any] = {}
    if len(closes) < 30:
        return 0, {"reason": "not_enough_history", "len": len(closes)}

    lookback = int(cfg["lookback_hours"])
    dip_window = closes[-min(len(closes), lookback):]
    high, dip_pct = dip_stats(spot, dip_window)
    details["lookback_high"] = high
    details["dip_pct"] = dip_pct

    r = rsi(closes, int(cfg["rsi_period"]))
    details["rsi"] = r

    sma_hours = int(cfg["trend_sma_hours"])
    trend_window = closes[-min(len(closes), sma_hours):]
    s = sma(trend_window)
    details["sma"] = s
    details["pct_vs_sma"] = pct_from(spot, s) if s else None

    m_fast = momentum_pct(closes, int(cfg["mom_fast_hours"]))
    m_slow = momentum_pct(closes, int(cfg["mom_slow_hours"]))
    details["mom_fast_pct"] = m_fast
    details["mom_slow_pct"] = m_slow

    score = 0

    # Dip points (0..40)
    dip_points = 0
    if dip_pct is not None:
        d = abs(min(0.0, dip_pct))
        dip_points = int(min(40, (d / 5.0) * 40))
    score += dip_points
    details["dip_points"] = dip_points

    # RSI points (0..35)
    rsi_points = 0
    if r is not None:
        if r <= cfg["rsi_very_oversold"]:
            rsi_points = 35
        elif r <= cfg["rsi_oversold"]:
            rsi_points = 25
        elif r <= 35:
            rsi_points = 12
    score += rsi_points
    details["rsi_points"] = rsi_points

    # Trend points (-10..15)
    trend_points = 0
    pct_vs = details.get("pct_vs_sma")
    if pct_vs is not None:
        if pct_vs >= 0:
            trend_points = 15
        elif pct_vs >= -0.5:
            trend_points = 10
        elif pct_vs >= -1.5:
            trend_points = 5
        else:
            trend_points = -10
    score += trend_points
    details["trend_points"] = trend_points

    # Momentum points (-10..10)
    mom_points = 0
    if m_fast is not None and m_slow is not None:
        if m_fast > 0 and m_slow > 0:
            mom_points = 10
        elif m_fast > -0.5:
            mom_points = 5
        else:
            mom_points = -10
    score += mom_points
    details["mom_points"] = mom_points

    score = max(0, min(100, score))
    details["score"] = score
    return score, details


# -----------------------------
# Extra guards + sizing
# -----------------------------
def kill_switch_ok(current: float, closes_7d: List[float], threshold_pct: float) -> Tuple[bool, Optional[float], Optional[float]]:
    if not closes_7d:
        return True, None, None
    high = max(closes_7d)
    if high <= 0:
        return True, None, None
    dd_pct = (current - high) / high * 100.0
    return (dd_pct > threshold_pct), high, dd_pct


def dip_scaled_amount(base_usd: float, dip_pct: float, full_mult_at_pct: float, max_mult: float) -> float:
    if dip_pct >= 0:
        return base_usd
    denom = abs(full_mult_at_pct) if full_mult_at_pct != 0 else 1.0
    t = min(1.0, abs(dip_pct) / denom)
    mult = 1.0 + (max_mult - 1.0) * t
    return base_usd * mult


# -----------------------------
# Coinbase client + orders
# -----------------------------
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


# -----------------------------
# Upgrade 4: logging + stats
# -----------------------------
def estimate_update_position(st: Dict[str, Any], usd_amount: float, spot_price: float) -> None:
    """
    This tracks an estimated position based on decision-time price.
    It will not perfectly match real fills, but it's great for learning + tuning.
    """
    if spot_price <= 0:
        return

    est_qty = float(usd_amount) / float(spot_price)

    st["est_total_usd_spent"] = float(st.get("est_total_usd_spent", 0.0)) + float(usd_amount)
    st["est_total_base_qty"] = float(st.get("est_total_base_qty", 0.0)) + float(est_qty)

    qty = float(st.get("est_total_base_qty", 0.0))
    spent = float(st.get("est_total_usd_spent", 0.0))
    st["est_avg_entry"] = (spent / qty) if qty > 0 else None


def print_performance(st: Dict[str, Any], spot_price: float, product_id: str) -> None:
    qty = float(st.get("est_total_base_qty", 0.0))
    spent = float(st.get("est_total_usd_spent", 0.0))
    avg = st.get("est_avg_entry", None)

    if qty <= 0 or spent <= 0 or spot_price <= 0:
        print("[PERF] No estimated position yet.")
        return

    value = qty * spot_price
    pnl = value - spent
    pnl_pct = (pnl / spent) * 100.0 if spent > 0 else 0.0

    avg_txt = f"${float(avg):,.2f}" if isinstance(avg, (int, float)) else "n/a"
    print(f"[PERF] Est position for {product_id}:")
    print(f"[PERF]   est_qty: {qty:.8f}")
    print(f"[PERF]   est_avg_entry: {avg_txt}")
    print(f"[PERF]   est_spent: ${spent:,.2f}")
    print(f"[PERF]   est_value_now: ${value:,.2f}")
    print(f"[PERF]   est_pnl: ${pnl:,.2f} ({pnl_pct:.2f}%)")


def log_run(cfg: Dict[str, Any], st: Dict[str, Any], payload: Dict[str, Any]) -> None:
    if not cfg.get("log_each_run", True):
        return
    append_jsonl(LOG_PATH, payload)


# -----------------------------
# Main
# -----------------------------
def main() -> None:
    print("Bot started")

    cfg: Dict[str, Any]
    st: Dict[str, Any]
    try:
        cfg = load_config()
    except Exception as e:
        print(str(e))
        print("Bot finished successfully.")
        return

    st = load_state()
    reset_week_if_needed(st)

    today = iso_date()
    now_ts = iso_ts()

    # Base log payload (we’ll fill in as we go)
    log_payload: Dict[str, Any] = {
        "ts": now_ts,
        "date": today,
        "product_id": cfg.get("product_id"),
        "dry_run": bool(cfg.get("dry_run")),
        "decision": "UNKNOWN",
        "reason": "",
        "spot": None,
        "usd_amount": None,
        "ai_score": None,
        "ai_details": None,
    }

    # pause
    if cfg.get("pause_trading", False):
        log_payload.update({"decision": "SKIP", "reason": "pause_trading=true"})
        print("Decision: SKIP | pause_trading=true")
        st["last_run_date"] = today
        save_json(STATE_PATH, st)
        log_run(cfg, st, log_payload)
        print("Bot finished successfully.")
        return

    # allowed days
    weekday = utc_now().weekday()
    if weekday not in cfg.get("trade_days_utc", [0,1,2,3,4,5,6]):
        log_payload.update({"decision": "SKIP", "reason": f"weekday_not_allowed:{weekday}"})
        print(f"Decision: SKIP | Not an allowed trade day (weekday={weekday})")
        st["last_run_date"] = today
        save_json(STATE_PATH, st)
        log_run(cfg, st, log_payload)
        print("Bot finished successfully.")
        return

    # buys/day
    buys_by_day = st.get("buys_by_day", {})
    if not isinstance(buys_by_day, dict):
        buys_by_day = {}
    buys_today = int(buys_by_day.get(today, 0))
    if buys_today >= int(cfg.get("max_buys_per_day", 1)):
        log_payload.update({"decision": "SKIP", "reason": "max_buys_per_day"})
        print(f"Decision: SKIP | Max buys/day reached ({buys_today}/{cfg.get('max_buys_per_day')})")
        st["last_run_date"] = today
        st["buys_by_day"] = buys_by_day
        save_json(STATE_PATH, st)
        log_run(cfg, st, log_payload)
        print("Bot finished successfully.")
        return

    # run once/day (optional)
    if cfg.get("run_once_per_day", True) and st.get("last_run_date") == today:
        log_payload.update({"decision": "SKIP", "reason": "already_ran_today"})
        print(f"Already ran today ({today}). Exiting.")
        log_run(cfg, st, log_payload)
        print("Bot finished successfully.")
        return

    product_id = cfg["product_id"]

    # spot price
    try:
        spot = get_spot_price(product_id)
        log_payload["spot"] = spot
        print(f"Current spot price for {product_id}: ${spot:,.2f}")
    except Exception as e:
        log_payload.update({"decision": "SKIP", "reason": f"spot_error:{e}"})
        print(f"MARKET DATA ERROR: Could not fetch spot price: {e}")
        st["last_run_date"] = today
        save_json(STATE_PATH, st)
        log_run(cfg, st, log_payload)
        print("Bot finished successfully.")
        return

    # weekly cap
    weekly_spent = float(st.get("weekly_spent", 0.0))
    weekly_remaining = max(0.0, cfg["max_usd_per_week"] - weekly_spent)
    print(f"Weekly spent: ${weekly_spent:,.2f} / ${cfg['max_usd_per_week']:,.2f} (remaining ${weekly_remaining:,.2f})")
    if weekly_remaining < cfg["min_order_usd"]:
        log_payload.update({"decision": "SKIP", "reason": "weekly_limit"})
        print("Decision: SKIP | Weekly limit reached (or remaining too small).")
        st["last_run_date"] = today
        save_json(STATE_PATH, st)
        log_run(cfg, st, log_payload)
        print("Bot finished successfully.")
        return

    # cooldown
    last_buy_ts = int(st.get("last_buy_ts", 0))
    if last_buy_ts > 0:
        hours_since = (time.time() - last_buy_ts) / 3600.0
        if hours_since < cfg["cooldown_hours"]:
            log_payload.update({"decision": "SKIP", "reason": "cooldown"})
            print(f"Decision: SKIP | Cooldown active ({hours_since:.1f}h since last buy, need {cfg['cooldown_hours']}h).")
            st["last_run_date"] = today
            save_json(STATE_PATH, st)
            log_run(cfg, st, log_payload)
            print_performance(st, spot, product_id)
            print("Bot finished successfully.")
            return

    # candles history
    history_hours = int(cfg.get("history_hours", 200))
    try:
        closes = get_hourly_closes(product_id, history_hours)
    except Exception as e:
        closes = []
        log_payload.update({"decision": "SKIP", "reason": f"candles_error:{e}"})
        print(f"MARKET DATA ERROR: Could not fetch candles: {e}")

    if not closes:
        log_payload.update({"decision": "SKIP", "reason": "no_history"})
        print("Decision: SKIP | No candle history available")
        st["last_run_date"] = today
        save_json(STATE_PATH, st)
        log_run(cfg, st, log_payload)
        print_performance(st, spot, product_id)
        print("Bot finished successfully.")
        return

    # kill switch
    if cfg.get("kill_switch_enabled", True):
        try:
            closes_7d = get_hourly_closes(product_id, int(cfg["kill_switch_lookback_hours"]))
        except Exception:
            closes_7d = []
        ok, high7, dd_pct = kill_switch_ok(spot, closes_7d, float(cfg["kill_switch_drawdown_pct"]))
        if high7 is not None and dd_pct is not None:
            print(f"Kill-switch 7d high: ${high7:,.2f} | drawdown={dd_pct:.2f}% (threshold {cfg['kill_switch_drawdown_pct']}%)")
        if not ok:
            log_payload.update({"decision": "SKIP", "reason": "kill_switch"})
            print("Decision: SKIP | Kill-switch triggered")
            st["last_run_date"] = today
            save_json(STATE_PATH, st)
            log_run(cfg, st, log_payload)
            print_performance(st, spot, product_id)
            print("Bot finished successfully.")
            return

    # AI score
    threshold = int(cfg.get("ai_score_threshold", 70))
    score, details = compute_ai_score(spot, closes, cfg)
    log_payload["ai_score"] = score
    log_payload["ai_details"] = details

    if cfg.get("ai_debug_print", True):
        print(f"AI Score: {score}/100 (threshold {threshold})")
        for k in ["dip_pct", "rsi", "pct_vs_sma", "mom_fast_pct", "mom_slow_pct", "dip_points", "rsi_points", "trend_points", "mom_points"]:
            if k in details:
                print(f"  {k}: {details[k]}")

    if score < threshold:
        log_payload.update({"decision": "SKIP", "reason": "ai_below_threshold"})
        print("Decision: SKIP | AI score below threshold")
        st["last_run_date"] = today
        save_json(STATE_PATH, st)
        log_run(cfg, st, log_payload)
        print_performance(st, spot, product_id)
        print("Bot finished successfully.")
        return

    # Amount sizing
    dip_pct = details.get("dip_pct")
    usd_amount = float(cfg["usd_per_day"])
    if cfg.get("dip_scaling_enabled", True) and isinstance(dip_pct, (int, float)):
        usd_amount = dip_scaled_amount(
            base_usd=float(cfg["usd_per_day"]),
            dip_pct=float(dip_pct),
            full_mult_at_pct=float(cfg["dip_scaling_full_mult_at_pct"]),
            max_mult=float(cfg["dip_scaling_max_mult"]),
        )
    usd_amount = min(float(usd_amount), weekly_remaining)
    usd_amount = max(float(cfg["min_order_usd"]), min(float(usd_amount), float(cfg["max_order_usd"])))
    log_payload["usd_amount"] = usd_amount

    print("Decision: BUY")

    # DRY RUN
    if bool(cfg["dry_run"]):
        log_payload.update({"decision": "BUY", "reason": "dry_run"})
        print(f"[DRY RUN] Would buy ${usd_amount:,.2f} of {product_id}")

        # Optional: simulate position for learning (keeps your stats meaningful)
        estimate_update_position(st, usd_amount, spot)

        st["last_run_date"] = today
        save_json(STATE_PATH, st)
        log_run(cfg, st, log_payload)
        print_performance(st, spot, product_id)
        print("Bot finished successfully.")
        return

    # LIVE mode
    client = make_client()
    if client is None:
        log_payload.update({"decision": "SKIP", "reason": "no_client_live"})
        print("[LIVE MODE] ERROR: No Coinbase client available.")
        st["last_run_date"] = today
        save_json(STATE_PATH, st)
        log_run(cfg, st, log_payload)
        print_performance(st, spot, product_id)
        print("Bot finished successfully.")
        return

    try:
        print("[STAGE 6] LIVE MODE enabled. Placing real order...")
        resp = place_market_buy(client, product_id, usd_amount)
        order_id = resp.get("order_id") or resp.get("orderId") or resp.get("id")
        log_payload.update({"decision": "BUY", "reason": "live_order", "order_id": order_id})

        print("[STAGE 6] Order placed.")
        if order_id:
            print(f"[STAGE 6] order_id={order_id}")

        st["weekly_spent"] = float(st.get("weekly_spent", 0.0)) + float(usd_amount)
        st["last_buy_ts"] = int(time.time())
        st["last_buy_price"] = float(spot)

        buys_by_day[today] = int(buys_by_day.get(today, 0)) + 1
        st["buys_by_day"] = buys_by_day

        # Update estimated stats too (helps you monitor performance)
        estimate_update_position(st, usd_amount, spot)

    except Exception as e:
        log_payload.update({"decision": "SKIP", "reason": f"order_failed:{e}"})
        print("[STAGE 6] Order FAILED.")
        print(f"Error: {e}")

    st["last_run_date"] = today
    save_json(STATE_PATH, st)
    log_run(cfg, st, log_payload)
    print_performance(st, spot, product_id)
    print("Bot finished successfully.")


if __name__ == "__main__":
    main()

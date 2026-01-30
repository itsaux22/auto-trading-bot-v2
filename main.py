import os
import json
import time
import uuid
import math
from datetime import datetime, timezone

# Coinbase Advanced Trade SDK
# pip install coinbase-advanced-py
from coinbase.rest import RESTClient


# ----------------------------
# Helpers
# ----------------------------
def utc_today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def iso_week_key(dt: datetime) -> str:
    # e.g., "2026-W03"
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def must_get(cfg: dict, key: str):
    if key not in cfg:
        raise KeyError(key)
    return cfg[key]


def to_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default


def clamp(n, lo, hi):
    return max(lo, min(hi, n))


# ----------------------------
# Coinbase wrappers
# ----------------------------
def make_client() -> RESTClient:
    api_key = os.getenv("COINBASE_API_KEY", "").strip()
    api_secret = os.getenv("COINBASE_API_SECRET", "").strip()

    if not api_key or not api_secret:
        raise RuntimeError("Missing COINBASE_API_KEY or COINBASE_API_SECRET env vars.")

    # api_secret should be the PEM private key text (with BEGIN/END lines + newlines)
    return RESTClient(api_key=api_key, api_secret=api_secret)


def read_only_check(client: RESTClient) -> int:
    # If this works, auth is valid.
    # The SDK returns an object; we just want to see if it doesn't throw.
    resp = client.list_accounts()
    # Best-effort count
    try:
        accounts = getattr(resp, "accounts", None)
        if accounts is None:
            return 0
        return len(accounts)
    except Exception:
        return 0


def get_spot_price(client: RESTClient, product_id: str) -> float | None:
    try:
        product = client.get_product(product_id)
        # SDK object may have different shapes; try common fields
        price = getattr(product, "price", None)
        if price is None and isinstance(product, dict):
            price = product.get("price")
        return to_float(price, None)
    except Exception:
        return None


def get_hourly_closes(client: RESTClient, product_id: str, hours: int) -> list[float]:
    """
    Pull hourly candles and return closes (oldest -> newest).
    Coinbase candle endpoint may return:
      - dict with "candles"
      - object with .candles
    Each candle might be dict-like with "close".
    """
    end = int(time.time())
    start = end - hours * 3600

    try:
        resp = client.get_product_candles(
            product_id=product_id,
            start=str(start),
            end=str(end),
            granularity="ONE_HOUR",
        )
    except Exception:
        return []

    candles = None
    if isinstance(resp, dict):
        candles = resp.get("candles")
    else:
        candles = getattr(resp, "candles", None)

    if not candles:
        return []

    closes = []
    for c in candles:
        if isinstance(c, dict):
            close = c.get("close")
        else:
            close = getattr(c, "close", None)

        close_f = to_float(close, None)
        if close_f is not None and close_f > 0:
            closes.append(close_f)

    # Coinbase often returns newest -> oldest; we want oldest -> newest
    closes = list(reversed(closes))
    return closes


def pct_change(new: float, old: float) -> float:
    if old == 0:
        return 0.0
    return (new - old) / old * 100.0


def get_7d_volatility(client: RESTClient, product_id: str) -> float | None:
    closes = get_hourly_closes(client, product_id, hours=7 * 24)
    if len(closes) < 30:
        return None
    rets = []
    for i in range(1, len(closes)):
        r = (closes[i] / closes[i - 1]) - 1.0
        if math.isfinite(r):
            rets.append(r)
    if len(rets) < 10:
        return None
    # daily-ish volatility estimate from hourly returns
    # stdev * sqrt(24*7) gives 7-day volatility scale
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    stdev = math.sqrt(max(var, 0.0))
    vol = stdev * math.sqrt(24 * 7)
    return vol


def place_market_buy_usd(client: RESTClient, product_id: str, usd_amount: float) -> dict:
    client_order_id = str(uuid.uuid4())
    # Coinbase SDK order shape:
    # order_configuration.market_market_ioc.quote_size = "<usd>"
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

    # Normalize response to dict-ish output
    out = {
        "client_order_id": client_order_id,
        "order_id": None,
        "raw": None,
    }

    try:
        if isinstance(resp, dict):
            out["raw"] = resp
            out["order_id"] = resp.get("order_id") or resp.get("id")
        else:
            out["raw"] = str(resp)
            out["order_id"] = getattr(resp, "order_id", None) or getattr(resp, "id", None)
    except Exception:
        pass

    return out


# ----------------------------
# Strategy / Bot
# ----------------------------
REQUIRED_CONFIG_KEYS = [
    "product_id",
    "usd_per_day",
    "max_usd_per_week",
    "dry_run",
]

DEFAULT_CONFIG = {
    # REQUIRED:
    "product_id": "BTC-USD",
    "usd_per_day": 10,
    "max_usd_per_week": 70,
    "dry_run": True,

    # OPTIONAL / recommended:
    "run_once_per_day": True,          # prevents multiple buys same day
    "dip_threshold_pct": -1.0,         # buy when price is <= (24h change) threshold (negative means dip)
    "lookback_hours": 24,              # used to compare current vs past close
    "min_volatility": 0.0,             # set >0 to only trade when vol is high enough
    "max_volatility": 1.0,             # set lower to avoid extreme volatility
    "cooldown_hours": 6,               # minimum time between buys
    "min_order_usd": 5.00,             # don't place tiny orders
    "max_order_usd": 50.00,            # safety cap per order
}


def load_config(path="config.json") -> dict:
    cfg = load_json(path, DEFAULT_CONFIG.copy())
    # Ensure defaults exist
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)

    missing = [k for k in REQUIRED_CONFIG_KEYS if k not in cfg]
    if missing:
        print(f"CONFIG ERROR: Missing keys: {', '.join(missing)}")
        # Show the template the user should paste
        print("CONFIG TEMPLATE (config.json) should look like:")
        print(json.dumps(DEFAULT_CONFIG, indent=2))
        return {}

    # Normalize types
    cfg["usd_per_day"] = float(cfg["usd_per_day"])
    cfg["max_usd_per_week"] = float(cfg["max_usd_per_week"])
    cfg["dry_run"] = bool(cfg["dry_run"])
    cfg["run_once_per_day"] = bool(cfg.get("run_once_per_day", True))
    cfg["dip_threshold_pct"] = float(cfg.get("dip_threshold_pct", -1.0))
    cfg["lookback_hours"] = int(cfg.get("lookback_hours", 24))
    cfg["cooldown_hours"] = int(cfg.get("cooldown_hours", 6))
    cfg["min_order_usd"] = float(cfg.get("min_order_usd", 5.0))
    cfg["max_order_usd"] = float(cfg.get("max_order_usd", 50.0))
    cfg["min_volatility"] = float(cfg.get("min_volatility", 0.0))
    cfg["max_volatility"] = float(cfg.get("max_volatility", 1.0))

    return cfg


def load_state(path="state.json") -> dict:
    return load_json(path, {
        "week_key": None,
        "weekly_spent": 0.0,
        "last_run_date": None,
        "last_buy_ts": None,
        "last_buy_price": None,
        "last_client_order_id": None,
        "last_order_id": None
    })


def reset_week_if_needed(state: dict):
    now = datetime.now(timezone.utc)
    wk = iso_week_key(now)
    if state.get("week_key") != wk:
        state["week_key"] = wk
        state["weekly_spent"] = 0.0


def should_cooldown(state: dict, cooldown_hours: int) -> bool:
    ts = state.get("last_buy_ts")
    if not ts:
        return False
    try:
        last = float(ts)
    except Exception:
        return False
    return (time.time() - last) < (cooldown_hours * 3600)


def main():
    print("Bot started")

    cfg = load_config("config.json")
    if not cfg:
        print("Bot finished successfully.")
        return

    product_id = cfg["product_id"]
    usd_per_day = cfg["usd_per_day"]
    max_usd_per_week = cfg["max_usd_per_week"]
    dry_run = cfg["dry_run"]

    state_path = "state.json"
    state = load_state(state_path)
    reset_week_if_needed(state)

    # Optional "run once per day"
    today = utc_today_str()
    if cfg.get("run_once_per_day", True) and state.get("last_run_date") == today:
        print(f"Already ran today ({today}). Exiting.")
        print("Bot finished successfully.")
        return

    # Connect + auth test
    client = make_client()

    print("[STAGE 5] Coinbase keys detected. Trying read-only account list...")
    try:
        count = read_only_check(client)
        print(f"[STAGE 5] Read-only check OK. accounts_count={count}")
    except Exception as e:
        print("[STAGE 5] Read-only check FAILED.")
        print(f"Error: {e}")
        # still continue in dry run mode to show decision logic
        count = 0

    price = get_spot_price(client, product_id)
    if price is None:
        print(f"ERROR: Could not fetch spot price for {product_id}.")
        state["last_run_date"] = today
        save_json(state_path, state)
        print("Bot finished successfully.")
        return

    print(f"Current spot price for {product_id}: ${price:,.2f}")

    weekly_spent = float(state.get("weekly_spent", 0.0) or 0.0)
    remaining_week = max(0.0, max_usd_per_week - weekly_spent)
    print(f"Weekly spent: ${weekly_spent:,.2f} / ${max_usd_per_week:,.2f} (remaining ${remaining_week:,.2f})")

    # Decide order size (safety caps)
    order_usd = clamp(usd_per_day, cfg["min_order_usd"], cfg["max_order_usd"])
    order_usd = min(order_usd, remaining_week)

    if order_usd < cfg["min_order_usd"]:
        print("Decision: SKIP | Weekly limit reached (or order too small).")
        state["last_run_date"] = today
        save_json(state_path, state)
        print("Bot finished successfully.")
        return

    # Cooldown between buys
    if should_cooldown(state, cfg["cooldown_hours"]):
        print(f"Decision: SKIP | Cooldown active ({cfg['cooldown_hours']}h).")
        state["last_run_date"] = today
        save_json(state_path, state)
        print("Bot finished successfully.")
        return

    # Volatility filter (optional)
    vol = get_7d_volatility(client, product_id)
    if vol is not None:
        if vol < cfg["min_volatility"] or vol > cfg["max_volatility"]:
            print(f"Decision: SKIP | Volatility out of range. 7d_vol={vol:.4f}")
            state["last_run_date"] = today
            save_json(state_path, state)
            print("Bot finished successfully.")
            return

    # Dip check using lookback close
    lookback_h = max(1, int(cfg["lookback_hours"]))
    closes = get_hourly_closes(client, product_id, hours=lookback_h + 2)
    if len(closes) < 2:
        # No candle data -> safest is skip unless you want always-buy DCA
        # Here we do "buy allowed" only if dip_threshold_pct >= 0 (meaning you want DCA always).
        if cfg["dip_threshold_pct"] < 0:
            print("Decision: SKIP | Not enough candle data for dip check.")
            state["last_run_date"] = today
            save_json(state_path, state)
            print("Bot finished successfully.")
            return
        else:
            print("Decision: BUY | No candles, but DCA mode enabled.")
    else:
        past = closes[0]
        change = pct_change(price, past)
        threshold = cfg["dip_threshold_pct"]
        if change <= threshold:
            print(f"Decision: BUY | Dip detected ({change:.2f}% over ~{lookback_h}h, threshold {threshold:.2f}%)")
        else:
            print(f"Decision: SKIP | Not a dip yet ({change:.2f}% over ~{lookback_h}h, threshold {threshold:.2f}%)")
            state["last_run_date"] = today
            save_json(state_path, state)
            print("Bot finished successfully.")
            return

    # Execute
    if dry_run:
        print(f"[DRY RUN] Would buy ${order_usd:.2f} of {product_id}")
        state["last_run_date"] = today
        save_json(state_path, state)
        print("Bot finished successfully.")
        return

    print("[STAGE 6] LIVE MODE enabled. Placing real order...")
    try:
        res = place_market_buy_usd(client, product_id, order_usd)
        print("[STAGE 6] Order placed.")
        print(f"[STAGE 6] client_order_id={res.get('client_order_id')}")
        print(f"[STAGE 6] order_id={res.get('order_id')}")
        # Update state
        state["weekly_spent"] = float(state.get("weekly_spent", 0.0) or 0.0) + float(order_usd)
        state["last_buy_ts"] = time.time()
        state["last_buy_price"] = price
        state["last_client_order_id"] = res.get("client_order_id")
        state["last_order_id"] = res.get("order_id")
    except Exception as e:
        print("[STAGE 6] Order FAILED.")
        print(f"Error: {e}")

    state["last_run_date"] = today
    save_json(state_path, state)
    print("Bot finished successfully.")


if __name__ == "__main__":
    main()

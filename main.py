import os
import json
import time
import uuid
from datetime import datetime, timezone, timedelta

import requests
import jwt  # PyJWT


CONFIG_PATH = "config.json"
STATE_PATH = "state.json"

COINBASE_API_KEY = os.getenv("COINBASE_API_KEY", "").strip()
COINBASE_API_SECRET = os.getenv("COINBASE_API_SECRET", "").strip()  # PEM private key string

API_BASE = "https://api.coinbase.com"
BROKERAGE_BASE = f"{API_BASE}/api/v3/brokerage"


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def now_utc():
    return datetime.now(timezone.utc)


def get_today_utc_str():
    return now_utc().strftime("%Y-%m-%d")


def get_iso_week_key(dt: datetime):
    # ISO week: (year, weeknum)
    iso = dt.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def build_jwt(method: str, path: str) -> str:
    """
    Coinbase Cloud / CDP style JWT auth:
    - sub = API key id
    - uri = "{METHOD} {PATH}" where PATH includes /api/v3/... etc
    - sign with ES256 private key (PEM)
    """
    if not COINBASE_API_KEY or not COINBASE_API_SECRET:
        return ""

    iat = int(time.time())
    exp = iat + 60  # short-lived
    uri = f"{method.upper()} {path}"

    payload = {
        "sub": COINBASE_API_KEY,
        "iss": "coinbase-cloud",
        "nbf": iat,
        "exp": exp,
        "uri": uri,
    }

    headers = {
        "kid": COINBASE_API_KEY,
        "nonce": str(uuid.uuid4()),
        "typ": "JWT",
    }

    token = jwt.encode(payload, COINBASE_API_SECRET, algorithm="ES256", headers=headers)
    # pyjwt may return bytes or str depending on version
    if isinstance(token, bytes):
        token = token.decode("utf-8")
    return token


def cb_request(method: str, path: str, json_body=None, timeout=30):
    url = f"{API_BASE}{path}"
    token = build_jwt(method, path)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    r = requests.request(method, url, headers=headers, json=json_body, timeout=timeout)
    return r


def public_spot_price(product_id: str) -> float:
    """
    Simple public spot price (no auth) using Coinbase spot endpoint.
    This is fine for a reference price. Execution price may differ slightly.
    """
    # product_id is like BTC-USD -> currency pair
    base, quote = product_id.split("-")
    url = f"{API_BASE}/v2/prices/{base}-{quote}/spot"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()
    return float(data["data"]["amount"])


def stage5_read_only_check():
    print("[STAGE 5] Coinbase keys detected. Trying read-only account list...")
    try:
        r = cb_request("GET", "/api/v3/brokerage/accounts")
        if r.status_code != 200:
            print(f"[STAGE 5] Read-only check FAILED.")
            print(f"Error: Coinbase API error {r.status_code}: {r.text[:300]}")
            return False

        data = r.json()
        accounts = data.get("accounts", [])
        print(f"[STAGE 5] Read-only check OK. accounts_count={len(accounts)}")
        return True
    except Exception as e:
        print("[STAGE 5] Read-only check FAILED.")
        print(f"Error: {e}")
        return False


def place_market_buy(product_id: str, usd_amount: float, dry_run: bool):
    if dry_run:
        print(f"[DRY RUN] Would market BUY ${usd_amount:.2f} of {product_id}")
        return {"dry_run": True}

    client_order_id = str(uuid.uuid4())

    body = {
        "client_order_id": client_order_id,
        "product_id": product_id,
        "side": "BUY",
        "order_configuration": {
            "market_market_ioc": {
                "quote_size": f"{usd_amount:.2f}"
            }
        }
    }

    r = cb_request("POST", "/api/v3/brokerage/orders", json_body=body)
    if r.status_code != 200:
        raise RuntimeError(f"BUY failed {r.status_code}: {r.text[:400]}")

    resp = r.json()
    # Depending on API, ids can appear in different fields. Print key ones.
    order_id = resp.get("order_id")
    success = resp.get("success")
    print("[STAGE 6] Order placed.")
    print(f"[STAGE 6] client_order_id={client_order_id}")
    print(f"[STAGE 6] order_id={order_id}")
    if success is not None:
        print(f"[STAGE 6] success={success}")
    return resp


def place_limit_sell(product_id: str, base_size: float, limit_price: float, dry_run: bool):
    if dry_run:
        print(f"[DRY RUN] Would LIMIT SELL {base_size:.8f} {product_id.split('-')[0]} at ${limit_price:.2f}")
        return {"dry_run": True}

    client_order_id = str(uuid.uuid4())
    body = {
        "client_order_id": client_order_id,
        "product_id": product_id,
        "side": "SELL",
        "order_configuration": {
            "limit_limit_gtc": {
                "base_size": f"{base_size:.8f}",
                "limit_price": f"{limit_price:.2f}"
            }
        }
    }

    r = cb_request("POST", "/api/v3/brokerage/orders", json_body=body)
    if r.status_code != 200:
        raise RuntimeError(f"SELL failed {r.status_code}: {r.text[:400]}")

    resp = r.json()
    print("[STAGE 7] Take-profit LIMIT sell placed.")
    print(f"[STAGE 7] client_order_id={client_order_id}")
    print(f"[STAGE 7] order_id={resp.get('order_id')}")
    return resp


def main():
    print("Bot started")

    config = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {})

    product_id = config.get("product_id", "BTC-USD")
    usd_per_day = float(config.get("usd_per_day", 10))
    max_usd_per_week = float(config.get("max_usd_per_week", 70))
    dry_run = bool(config.get("dry_run", True))

    min_drop_pct_to_buy = float(config.get("min_drop_pct_to_buy", 1.0))
    buy_if_no_last_price = bool(config.get("buy_if_no_last_price", True))

    enable_take_profit = bool(config.get("enable_take_profit", True))
    take_profit_pct = float(config.get("take_profit_pct", 1.5))
    take_profit_sell_pct = float(config.get("take_profit_sell_pct_of_position", 50))

    min_minutes_between_buys = int(config.get("min_minutes_between_buys", 120))
    max_total_usd_position = float(config.get("max_total_usd_position", 300))

    today = get_today_utc_str()
    week_key = get_iso_week_key(now_utc())

    # Init state defaults
    state.setdefault("week_key", week_key)
    state.setdefault("spent_this_week", 0.0)
    state.setdefault("last_run_day", "")
    state.setdefault("last_buy_price", None)
    state.setdefault("last_buy_time_utc", None)
    state.setdefault("estimated_base_position", 0.0)  # rough estimate for take-profit sells

    # Reset weekly spend if new week
    if state["week_key"] != week_key:
        state["week_key"] = week_key
        state["spent_this_week"] = 0.0

    # One-run-per-day guard
    if state.get("last_run_day") == today:
        print(f"Already ran today ({today}). Exiting.")
        return

    # Stage 5 auth check (read-only)
    stage5_read_only_check()

    # Get current spot price
    current_price = public_spot_price(product_id)
    print(f"Current spot price for {product_id}: ${current_price:,.2f}")

    # Weekly budget guard
    spent = float(state.get("spent_this_week", 0.0))
    remaining = max(0.0, max_usd_per_week - spent)
    print(f"Weekly spent: ${spent:.2f} / ${max_usd_per_week:.2f} (remaining ${remaining:.2f})")

    if remaining < usd_per_day:
        print("Decision: SKIP (weekly budget reached)")
        state["last_run_day"] = today
        save_json(STATE_PATH, state)
        print("Bot finished successfully.")
        return

    # Position cap guard (rough, based on estimated position value)
    est_pos = float(state.get("estimated_base_position", 0.0))
    est_pos_value = est_pos * current_price
    if est_pos_value >= max_total_usd_position:
        print(f"Decision: SKIP (position cap hit ~${est_pos_value:.2f} >= ${max_total_usd_position:.2f})")
        state["last_run_day"] = today
        save_json(STATE_PATH, state)
        print("Bot finished successfully.")
        return

    # Cooldown guard between buys
    last_buy_time = state.get("last_buy_time_utc")
    if last_buy_time:
        try:
            last_dt = datetime.fromisoformat(last_buy_time)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            mins = (now_utc() - last_dt).total_seconds() / 60.0
            if mins < min_minutes_between_buys:
                print(f"Decision: SKIP (cooldown {mins:.1f}m < {min_minutes_between_buys}m)")
                state["last_run_day"] = today
                save_json(STATE_PATH, state)
                print("Bot finished successfully.")
                return
        except Exception:
            pass

    # Buy decision
    last_price = state.get("last_buy_price")
    decision_buy = False

    if last_price is None:
        decision_buy = buy_if_no_last_price
        print("Decision:", "BUY" if decision_buy else "SKIP", "(no last_buy_price)")
    else:
        drop_pct = ((last_price - current_price) / last_price) * 100.0
        decision_buy = drop_pct >= min_drop_pct_to_buy
        print(f"Last buy price: ${last_price:,.2f} | Drop since last: {drop_pct:.2f}%")
        print("Decision:", "BUY" if decision_buy else "SKIP")

    # Execute buy + optional take-profit sell
    if decision_buy:
        if dry_run:
            print("[STAGE 6] DRY RUN mode.")
        else:
            print("[STAGE 6] LIVE MODE enabled. Placing real order...")

        buy_resp = place_market_buy(product_id, usd_per_day, dry_run=dry_run)

        # Estimate base size from spot price (approx)
        base_bought_est = usd_per_day / current_price
        state["estimated_base_position"] = float(state.get("estimated_base_position", 0.0)) + base_bought_est

        # Update spend + last buy info
        state["spent_this_week"] = float(state["spent_this_week"]) + usd_per_day
        state["last_buy_price"] = current_price
        state["last_buy_time_utc"] = now_utc().isoformat()

        # Stage 7: take-profit sell (limit)
        if enable_take_profit:
            sell_pct = max(0.0, min(100.0, take_profit_sell_pct))
            sell_base = base_bought_est * (sell_pct / 100.0)

            tp_price = current_price * (1.0 + take_profit_pct / 100.0)

            if sell_base > 0:
                if dry_run:
                    print("[STAGE 7] DRY RUN take-profit.")
                else:
                    print("[STAGE 7] LIVE take-profit enabled. Placing limit sell...")

                place_limit_sell(product_id, base_size=sell_base, limit_price=tp_price, dry_run=dry_run)

                # Reduce estimated position by amount we intend to sell
                state["estimated_base_position"] = max(
                    0.0, float(state.get("estimated_base_position", 0.0)) - sell_base
                )

    # Mark completed for today
    state["last_run_day"] = today
    save_json(STATE_PATH, state)
    print("Bot finished successfully.")


if __name__ == "__main__":
    main()

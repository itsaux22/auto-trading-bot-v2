import os
import json
import time
import uuid
from datetime import datetime, timezone

import requests
import jwt  # PyJWT


CONFIG_PATH = "config.json"
STATE_PATH = "state.json"

COINBASE_API_KEY = os.getenv("COINBASE_API_KEY", "").strip()
COINBASE_API_SECRET = os.getenv("COINBASE_API_SECRET", "").strip()  # PEM private key string

# Coinbase Advanced Trade base
CB_BASE = "https://api.coinbase.com"
CB_ACCOUNTS = "/api/v3/brokerage/accounts"
CB_ORDERS = "/api/v3/brokerage/orders"

# Public ticker (simple, no auth)
EXCHANGE_TICKER = "https://api.exchange.coinbase.com/products/{product_id}/ticker"


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def today_utc_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def week_id_utc():
    # ISO year-week, e.g. "2026-W02"
    now = datetime.now(timezone.utc)
    iso_year, iso_week, _ = now.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def get_spot_price(product_id: str) -> float:
    url = EXCHANGE_TICKER.format(product_id=product_id)
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    return float(data["price"])


def looks_like_pem_private_key(s: str) -> bool:
    return "BEGIN" in s and "PRIVATE KEY" in s and "END" in s


def build_cb_jwt(uri_path: str, method: str = "GET") -> str:
    """
    Coinbase Advanced Trade uses JWT signed with your CDP private key (ES256).
    """
    if not COINBASE_API_KEY or not COINBASE_API_SECRET:
        raise RuntimeError("Missing COINBASE_API_KEY / COINBASE_API_SECRET env vars.")

    if not looks_like_pem_private_key(COINBASE_API_SECRET):
        raise RuntimeError(
            "[STAGE 5] Coinbase secret does NOT look like a PEM private key.\n"
            "You probably need a CDP Advanced Trade API key (ECDSA PEM) for this."
        )

    now = int(time.time())
    payload = {
        "iss": "cdp",
        "sub": COINBASE_API_KEY,
        "nbf": now,
        "exp": now + 120,  # 2 minutes
        "uri": f"{method.upper()} {uri_path}",
    }

    token = jwt.encode(
        payload,
        COINBASE_API_SECRET,
        algorithm="ES256",
        headers={"kid": COINBASE_API_KEY},
    )
    return token


def cb_request(method: str, path: str, json_body=None):
    token = build_cb_jwt(path, method=method)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    url = CB_BASE + path
    r = requests.request(method.upper(), url, headers=headers, json=json_body, timeout=30)
    # Helpful debug if it fails
    if r.status_code >= 400:
        raise RuntimeError(f"Coinbase API error {r.status_code}: {r.text}")
    return r.json()


def stage5_read_only_check():
    """
    Read-only test: list accounts
    """
    print("[STAGE 5] Coinbase keys detected. Trying read-only account list...")
    try:
        resp = cb_request("GET", CB_ACCOUNTS)
        # resp is JSON dict; show small confirmation
        accounts = resp.get("accounts", [])
        print(f"[STAGE 5] Read-only check OK. accounts_count={len(accounts)}")
        return True
    except Exception as e:
        print("[STAGE 5] Read-only check FAILED.")
        print(f"Error: {e}")
        return False


def should_buy(config, state, current_price: float) -> bool:
    last_buy_price = state.get("last_buy_price")
    min_drop_pct = float(config.get("min_drop_pct_to_buy", 1.0))
    buy_if_no_last = bool(config.get("buy_if_no_last_price", True))

    if last_buy_price is None:
        return buy_if_no_last

    drop_pct = ((last_buy_price - current_price) / last_buy_price) * 100.0
    print(f"Last buy price: ${last_buy_price:,.2f} | Drop: {drop_pct:.2f}% | Need >= {min_drop_pct:.2f}%")
    return drop_pct >= min_drop_pct


def place_market_buy(product_id: str, usd_amount: float):
    """
    Place a market buy using quote_size (USD).
    """
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

    resp = cb_request("POST", CB_ORDERS, json_body=body)
    # Print a small summary
    print("[STAGE 6] Order placed.")
    print(f"[STAGE 6] client_order_id={client_order_id}")
    # Coinbase returns various fields; keep it safe and short:
    order_id = resp.get("success_response", {}).get("order_id") or resp.get("order_id")
    print(f"[STAGE 6] order_id={order_id}")
    return resp


def main():
    print("Bot started")

    config = load_json(CONFIG_PATH, default={})
    # Required keys
    required = ["product_id", "usd_per_day", "max_usd_per_week", "dry_run"]
    missing = [k for k in required if k not in config]
    if missing:
        print(f"CONFIG ERROR: Missing keys: {', '.join(missing)}")
        print("Bot finished successfully.")
        return

    product_id = config["product_id"]
    usd_per_day = float(config["usd_per_day"])
    max_usd_per_week = float(config["max_usd_per_week"])
    dry_run = bool(config["dry_run"])

    # State defaults (prevents KeyError like 'week')
    state = load_json(STATE_PATH, default={})
    state.setdefault("last_run_day", "")
    state.setdefault("week", "")
    state.setdefault("spent_this_week", 0.0)
    state.setdefault("last_buy_price", None)

    today = today_utc_str()
    week = week_id_utc()

    # Reset weekly budget when week changes
    if state["week"] != week:
        state["week"] = week
        state["spent_this_week"] = 0.0

    # Prevent multiple runs same day (optional safety)
    if state["last_run_day"] == today:
        print(f"Already ran today ({today}). Exiting.")
        save_json(STATE_PATH, state)
        print("Bot finished successfully.")
        return

    # Stage 5: verify auth if keys exist
    if COINBASE_API_KEY and COINBASE_API_SECRET:
        stage5_read_only_check()
    else:
        print("[STAGE 5] Coinbase keys not set in Secrets yet (COINBASE_API_KEY / COINBASE_API_SECRET).")

    # Fetch price
    current_price = get_spot_price(product_id)
    print(f"Current spot price for {product_id}: ${current_price:,.2f}")

    remaining_week = max_usd_per_week - float(state["spent_this_week"])
    print(f"Weekly spent: ${state['spent_this_week']:.2f} / ${max_usd_per_week:.2f} (remaining ${remaining_week:.2f})")

    # Budget checks
    if remaining_week < usd_per_day:
        print("Budget: Not enough remaining weekly budget to buy today.")
        state["last_run_day"] = today
        save_json(STATE_PATH, state)
        print("Bot finished successfully.")
        return

    # Decision
    buy_ok = should_buy(config, state, current_price)
    print(f"Decision: {'BUY' if buy_ok else 'SKIP'}")

    if buy_ok:
        if dry_run:
            print(f"[DRY RUN] Would buy ${usd_per_day:.2f} of {product_id}")
        else:
            print("[STAGE 6] LIVE MODE enabled. Placing real order...")
            place_market_buy(product_id, usd_per_day)
            state["spent_this_week"] = float(state["spent_this_week"]) + usd_per_day
            state["last_buy_price"] = current_price

    # Mark completed
    state["last_run_day"] = today
    save_json(STATE_PATH, state)
    print("Bot finished successfully.")


if __name__ == "__main__":
    main()
exit(main())

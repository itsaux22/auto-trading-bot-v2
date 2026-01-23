import os
import json
import time
import uuid
from datetime import datetime, timezone, timedelta

import requests
import jwt  # PyJWT

import secrets
from cryptography.hazmat.primitives import serialization


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
        json.dump(data, f, indent=2)


def now_utc():
    return datetime.now(timezone.utc)


def today_utc_str():
    return now_utc().strftime("%Y-%m-%d")


def week_id_utc():
    # ISO year-week, e.g. "2026-W02"
    iso_year, iso_week, _ = now_utc().isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def get_spot_price(product_id: str) -> float:
    url = EXCHANGE_TICKER.format(product_id=product_id)
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    return float(data["price"])


# -------------------------
# Coinbase JWT + Requests
# -------------------------
def build_cb_jwt(uri_path: str, method: str = "GET") -> str:
    """
    Coinbase Advanced Trade JWT:
    - sub and kid must be: organizations/{org_id}/apiKeys/{key_id}
    - uri must be: "{METHOD} api.coinbase.com{path}"
    """
    if not COINBASE_API_KEY or not COINBASE_API_SECRET:
        raise RuntimeError("Missing COINBASE_API_KEY / COINBASE_API_SECRET env vars.")

    key_name = COINBASE_API_KEY.strip()  # MUST be organizations/.../apiKeys/...
    key_secret = COINBASE_API_SECRET.strip().strip('"').strip("'").replace("\\n", "\n")

    if "organizations/" not in key_name or "/apiKeys/" not in key_name:
        raise RuntimeError(
            "COINBASE_API_KEY must be the full key name like:\n"
            "organizations/{org_id}/apiKeys/{key_id}"
        )

    private_key = serialization.load_pem_private_key(
        key_secret.encode("utf-8"),
        password=None
    )

    now = int(time.time())
    request_host = "api.coinbase.com"
    uri = f"{method.upper()} {request_host}{uri_path}"

    payload = {
        "sub": key_name,
        "iss": "cdp",
        "nbf": now,
        "exp": now + 120,
        "uri": uri,
    }

    token = jwt.encode(
        payload,
        private_key,
        algorithm="ES256",
        headers={
            "kid": key_name,
            "nonce": secrets.token_hex(16),
        },
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
    if r.status_code >= 400:
        raise RuntimeError(f"Coinbase API error {r.status_code}: {r.text}")
    return r.json()


def stage5_read_only_check():
    print("[STAGE 5] Coinbase keys detected. Trying read-only account list...")
    try:
        resp = cb_request("GET", CB_ACCOUNTS)
        accounts = resp.get("accounts", [])
        print(f"[STAGE 5] Read-only check OK. accounts_count={len(accounts)}")
        return True
    except Exception as e:
        print("[STAGE 5] Read-only check FAILED.")
        print(f"Error: {e}")
        return False


# -------------------------
# Trading logic (DCA dip buy)
# + Safety Rails Stage 1
# -------------------------
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
    client_order_id = str(uuid.uuid4())
    body = {
        "client_order_id": client_order_id,
        "product_id": product_id,
        "side": "BUY",
        "order_configuration": {
            "market_market_ioc": {"quote_size": f"{usd_amount:.2f}"}
        }
    }

    resp = cb_request("POST", CB_ORDERS, json_body=body)
    print("[STAGE 6] Order placed.")
    print(f"[STAGE 6] client_order_id={client_order_id}")
    order_id = resp.get("success_response", {}).get("order_id") or resp.get("order_id")
    print(f"[STAGE 6] order_id={order_id}")
    return resp


def main():
    print("Bot started")

    config = load_json(CONFIG_PATH, default={})

    # Required keys (existing)
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

    # -------------------------
    # Stage 1 Safety Rails
    # -------------------------
    kill_switch = bool(config.get("kill_switch", False))
    min_minutes_between_buys = int(config.get("min_minutes_between_buys", 180))
    max_total_usd_position = float(config.get("max_total_usd_position", 300))
    loss_trigger_pct = float(config.get("loss_trigger_pct", 3.0))
    cooldown_after_loss_hours = int(config.get("cooldown_after_loss_hours", 24))
    max_daily_spend = float(config.get("max_daily_spend", usd_per_day))  # default: 1 trade/day cap

    if kill_switch:
        print("[SAFETY] kill_switch=true. Exiting without trading.")
        print("Bot finished successfully.")
        return

    # State defaults
    state = load_json(STATE_PATH, default={})
    state.setdefault("last_run_day", "")
    state.setdefault("week", "")
    state.setdefault("spent_this_week", 0.0)
    state.setdefault("spent_today", 0.0)
    state.setdefault("last_spend_day", "")
    state.setdefault("last_buy_price", None)
    state.setdefault("last_buy_time_utc", None)

    # Track approximate position size in base units (we estimate using spot price)
    state.setdefault("est_base_position", 0.0)
    state.setdefault("pause_until_utc", None)  # if set, bot pauses until that time

    today = today_utc_str()
    week = week_id_utc()

    # Reset weekly spend on new week
    if state["week"] != week:
        state["week"] = week
        state["spent_this_week"] = 0.0

    # Reset daily spend when day changes
    if state["last_spend_day"] != today:
        state["last_spend_day"] = today
        state["spent_today"] = 0.0

    # If currently paused (cooldown after loss), exit early
    if state.get("pause_until_utc"):
        try:
            pause_until = datetime.fromisoformat(state["pause_until_utc"])
            if pause_until.tzinfo is None:
                pause_until = pause_until.replace(tzinfo=timezone.utc)
            if now_utc() < pause_until:
                remaining = pause_until - now_utc()
                hrs = remaining.total_seconds() / 3600.0
                print(f"[SAFETY] Bot is paused for cooldown after loss. Remaining ~{hrs:.1f} hours.")
                state["last_run_day"] = today
                save_json(STATE_PATH, state)
                print("Bot finished successfully.")
                return
        except Exception:
            # If parsing fails, clear pause to avoid stuck state
            state["pause_until_utc"] = None

    # Prevent multiple runs same day (your original safety)
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

    # -------------------------
    # Safety rail: position cap
    # -------------------------
    est_base_pos = float(state.get("est_base_position", 0.0))
    est_pos_value = est_base_pos * current_price
    print(f"[SAFETY] Est position value: ~${est_pos_value:.2f} (cap ${max_total_usd_position:.2f})")
    if est_pos_value >= max_total_usd_position:
        print("[SAFETY] Position cap hit. Skipping buys.")
        state["last_run_day"] = today
        save_json(STATE_PATH, state)
        print("Bot finished successfully.")
        return

    # -------------------------
    # Safety rail: cooldown between buys
    # -------------------------
    if state.get("last_buy_time_utc"):
        try:
            last_dt = datetime.fromisoformat(state["last_buy_time_utc"])
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            mins = (now_utc() - last_dt).total_seconds() / 60.0
            if mins < min_minutes_between_buys:
                print(f"[SAFETY] Buy cooldown active ({mins:.1f}m < {min_minutes_between_buys}m). Skipping.")
                state["last_run_day"] = today
                save_json(STATE_PATH, state)
                print("Bot finished successfully.")
                return
        except Exception:
            pass

    # -------------------------
    # Safety rail: loss-trigger pause (auto resume)
    # -------------------------
    last_buy_price = state.get("last_buy_price")
    if last_buy_price:
        drawdown_pct = ((last_buy_price - current_price) / last_buy_price) * 100.0
        if drawdown_pct >= loss_trigger_pct:
            pause_until = now_utc() + timedelta(hours=cooldown_after_loss_hours)
            state["pause_until_utc"] = pause_until.isoformat()
            print(
                f"[SAFETY] Loss trigger hit (drawdown {drawdown_pct:.2f}% >= {loss_trigger_pct:.2f}%). "
                f"Pausing for {cooldown_after_loss_hours}h."
            )
            state["last_run_day"] = today
            save_json(STATE_PATH, state)
            print("Bot finished successfully.")
            return

    # -------------------------
    # Budget checks (weekly + daily)
    # -------------------------
    remaining_week = max_usd_per_week - float(state["spent_this_week"])
    remaining_day = max_daily_spend - float(state["spent_today"])
    print(f"Weekly spent: ${state['spent_this_week']:.2f} / ${max_usd_per_week:.2f} (remain ${remaining_week:.2f})")
    print(f"Daily spent: ${state['spent_today']:.2f} / ${max_daily_spend:.2f} (remain ${remaining_day:.2f})")

    if remaining_week < usd_per_day:
        print("[SAFETY] Weekly budget too low to buy today.")
        state["last_run_day"] = today
        save_json(STATE_PATH, state)
        print("Bot finished successfully.")
        return

    if remaining_day < usd_per_day:
        print("[SAFETY] Daily spend cap reached. Skipping.")
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

        # Update budgets + state (even in dry-run we still update run day only;
        # but we should not update spends unless live)
        if not dry_run:
            state["spent_this_week"] = float(state["spent_this_week"]) + usd_per_day
            state["spent_today"] = float(state["spent_today"]) + usd_per_day

            # Update last buy tracking
            state["last_buy_price"] = current_price
            state["last_buy_time_utc"] = now_utc().isoformat()

            # Estimate base amount bought and add to position estimate
            est_base_bought = usd_per_day / current_price
            state["est_base_position"] = float(state.get("est_base_position", 0.0)) + est_base_bought

    # Mark completed
    state["last_run_day"] = today
    save_json(STATE_PATH, state)
    print("Bot finished successfully.")


if __name__ == "__main__":
    main()

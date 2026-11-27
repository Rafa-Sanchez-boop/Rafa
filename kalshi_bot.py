"""First-day Kalshi BTC15M quote alerts, preserving official-outcome learning; no automatic trading.
"""
import json
import logging
import math
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation
import requests
import btc_assistant

BASE = "https://external-api.kalshi.com/trade-api/v2"
SERIES = os.getenv("KALSHI_SERIES", "KXBTC15M")
ETH_SERIES = os.getenv("KALSHI_ETH_SERIES", "KXETH15M")
DB_PATH = os.getenv("DB_PATH", "/var/data/kalshi_bot.sqlite3")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SCAN_SECONDS = min(5, max(1, int(os.getenv("SCAN_SECONDS", "5"))))
COMBO_WINDOW_SECONDS = 250
OPENING_REVIEW_SECONDS = 0
COMBO_RULE_VERSION = "combo-window-250-kalshi-only-kraken-price-paused-next-contract-v3"
COMBO_QUALITY_FILTERS_ENABLED = False
COMBO_MIN_SETTLED = 30
COMBO_MIN_JOINT_ACCURACY = 0.90
COMBO_MIN_MARKET_PROBABILITY = Decimal("0.90")
MIN_VOLUME = Decimal("0")
BOT_VERSION = os.getenv("BOT_VERSION", "rafa-learning-history-btc-eth-next-contract-v1")
PANEL_URL = os.getenv("RAFA_PANEL_URL", "").rstrip("/")
PANEL_SECRET = os.getenv("RAFA_PANEL_SECRET", "")
PANEL_SITES_AUTH = os.getenv("RAFA_PANEL_SITES_AUTH", "")
RULES_SNAPSHOT = json.dumps({
    "series": SERIES, "eth_series": ETH_SERIES,
    "scan_seconds": SCAN_SECONDS, "opening_prediction_delay_seconds": OPENING_REVIEW_SECONDS,
    "combo_window_seconds": COMBO_WINDOW_SECONDS,
    "signal_rule": "At each BTC 15-minute opening, use the immediately previous official Kalshi result plus its complete YES quote path and historical cycles; Kraken supplies numeric BTC/USD values only",
    "opening_prediction_rule": "Send at the first active scan after Kalshi confirms the previous official result and publishes the new open contract; no 30-second wait",
    "combo_rule_version": COMBO_RULE_VERSION,
    "combo_quality_filters_enabled": COMBO_QUALITY_FILTERS_ENABLED,
    "combo_kraken_reference_enabled": False,
    "combo_rule": "Sample aligned BTC and ETH Kalshi 15-minute markets; Kalshi history and YES quotes drive both directions; Kraken price reference is paused for combo BTC",
    "combo_quality_gate": "Paused; preserve confidence and settlement data for evaluation without suppressing combo alerts",
    "learning": "Score new BTC and combo legs against Kalshi official settlement and retain feedback history",
    "automatic_orders": False,
}, separators=(",", ":"), sort_keys=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-only")
http = requests.Session()
http.headers["User-Agent"] = "KalshiBTC15MAdaptive/1.0"
# Extra in-process protection against duplicate alerts during a worker lifetime.
alerted_phases = set()
last_results_check = 0.0
KRAKEN_URL = "https://api.kraken.com/0/public/Ticker"
kraken_samples = []
kraken_last_quote = None
eth_kraken_samples = []
eth_kraken_last_quote = None
eth_kraken_5m_snapshot = None
btc_cycle_context_cache = None
btc_opening_context_cache = None

def kraken_trend(now):
    """Refresh the BTC/USD reference price and retain price samples only."""
    global kraken_last_quote
    try:
        response = http.get(KRAKEN_URL, params={"pair": "XBTUSD"}, timeout=10)
        response.raise_for_status()
        payload = response.json()
        if payload.get("error") or not payload.get("result"):
            log.warning("Kraken returned no valid BTC/USD ticker")
            return None
        item = next(iter(payload["result"].values()))
        price = dec(item["c"][0])
        if price is None or price <= 0:
            return None
        kraken_last_quote = (now, price)
        kraken_samples.append((now, price))
        kraken_samples[:] = [(t, p) for t, p in kraken_samples if now-t <= 600]
        return None
    except (requests.RequestException, ValueError, KeyError, IndexError,
            TypeError, StopIteration):
        log.exception("Kraken BTC/USD price reference failed")
        return None

kraken_5m_snapshot = None


def eth_kraken_trend(now):
    """Compare Kraken ETH/USD trades at least 60 seconds apart."""
    global eth_kraken_last_quote
    try:
        response = http.get(KRAKEN_URL, params={"pair": "XETHZUSD"}, timeout=10)
        response.raise_for_status()
        payload = response.json()
        if payload.get("error") or not payload.get("result"):
            return None
        item = next(iter(payload["result"].values()))
        price = dec(item["c"][0])
        if price is None or price <= 0:
            return None
        eth_kraken_last_quote = (now, price)
        eth_kraken_samples.append((now, price))
        eth_kraken_samples[:] = [(t, p) for t, p in eth_kraken_samples if now-t <= 600]
        prior = next((p for t, p in eth_kraken_samples if t <= now-60), None)
        if prior is None or price == prior:
            return None
        return "UP" if price > prior else "DOWN"
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError, StopIteration):
        log.exception("Kraken ETH/USD quote failed")
        return None


def eth_kraken_trend_5m(now):
    """Compare live ETH with a completed one-minute candle near five minutes ago."""
    global eth_kraken_5m_snapshot
    if eth_kraken_last_quote is None or now-eth_kraken_last_quote[0] > 30:
        return None
    try:
        response = http.get(KRAKEN_URL.replace("/Ticker", "/OHLC"),
                            params={"pair": "XETHZUSD", "interval": 1}, timeout=10)
        response.raise_for_status()
        payload = response.json()
        if payload.get("error") or not payload.get("result"):
            raise ValueError("Kraken ETH OHLC returned no data")
        result = payload["result"]
        key = next((name for name in result if name != "last"), None)
        candles = [c for c in result.get(key, []) if len(c) > 4] if key else []
        if not candles:
            raise ValueError("Kraken ETH OHLC contains no candles")
        target_time = now-300
        prior = min(candles, key=lambda c: abs(float(c[0])+60-target_time))
        prior_time = float(prior[0])+60
        current = eth_kraken_last_quote[1]
        prior_price = dec(prior[4])
        if abs(prior_time-target_time) > 45 or prior_price is None:
            raise ValueError("No ETH candle near the five-minute reference")
        eth_kraken_5m_snapshot = (now, prior_price, current)
        if prior_price == current:
            return None
        return "UP" if current > prior_price else "DOWN"
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError, StopIteration):
        log.warning("Kraken ETH five-minute OHLC unavailable; using recent samples")
        if not eth_kraken_samples:
            return None
        target_time = now-300
        prior_time, prior_price = min(eth_kraken_samples, key=lambda x: abs(x[0]-target_time))
        current_time, current = eth_kraken_samples[-1]
        if abs(prior_time-target_time) > 45 or current_time < now-30 or prior_price == current:
            return None
        eth_kraken_5m_snapshot = (now, prior_price, current)
        return "UP" if current > prior_price else "DOWN"


def eth_close_projection(now, remaining):
    """Project ETH from its fresh five-minute reference, capped at five minutes."""
    global eth_kraken_5m_snapshot
    if eth_kraken_last_quote is None or remaining < 0:
        return None
    quote_time, current = eth_kraken_last_quote
    if now-quote_time > 30:
        return None
    if eth_kraken_5m_snapshot and now-eth_kraken_5m_snapshot[0] <= 30:
        _, prior, _ = eth_kraken_5m_snapshot
    elif eth_kraken_samples:
        target_time = now-300
        prior_time, prior = min(eth_kraken_samples, key=lambda x: abs(x[0]-target_time))
        if abs(prior_time-target_time) > 45:
            return None
    else:
        return None
    if current == prior:
        return None
    change = current-prior
    if abs(change) < current*Decimal("0.0001"):
        return None
    horizon = min(max(Decimal("0"), Decimal(str(remaining))), Decimal("300"))
    projected = current + change*horizon/Decimal("300")
    return (current, projected) if projected > 0 else None


def eth_nearest_level_projection(now, direction, projection):
    """Use the same swing cap as the BTC forecast for the ETH combo leg."""
    if projection is None or eth_kraken_last_quote is None:
        return projection
    quote_time, current = eth_kraken_last_quote
    if now-quote_time > 30:
        return None
    try:
        response = http.get(KRAKEN_URL.replace("/Ticker", "/OHLC"),
                            params={"pair": "XETHZUSD", "interval": 1}, timeout=10)
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result") or {}
        key = next((name for name in result if name != "last"), None)
        candles = result.get(key, [])[-20:] if key else []
        highs = [dec(c[2]) for c in candles if len(c) > 4]
        lows = [dec(c[3]) for c in candles if len(c) > 4]
        highs = [p for p in highs if p is not None]
        lows = [p for p in lows if p is not None]
        if direction == "UP":
            levels = [p for p in highs if p > current]
            level = min(levels) if levels else None
            ceiling = max(highs) if highs else None
            if ceiling is not None and ceiling-current <= current*Decimal("0.0001"):
                return current, current
        else:
            levels = [p for p in lows if p < current]
            level = max(levels) if levels else None
            floor = min(lows) if lows else None
            if floor is not None and current-floor <= current*Decimal("0.0001"):
                return current, current
        if level is not None and abs(level-current) >= current*Decimal("0.0002"):
            target = min(projection[1], level) if direction == "UP" else max(projection[1], level)
            return current, target
        cap = current*Decimal("0.0025")
        move = projection[1]-current
        target = current + min(max(move, Decimal(0)), cap) if direction == "UP" else current-min(max(-move, Decimal(0)), cap)
        return current, target
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError, StopIteration):
        log.exception("Could not get nearby ETH swing levels; using conservative fallback")
        cap = current*Decimal("0.0025")
        move = projection[1]-current
        target = current + min(max(move, Decimal(0)), cap) if direction == "UP" else current-min(max(-move, Decimal(0)), cap)
        return current, target


def kraken_trend_5m(now):
    """Compare the live BTC quote with Kraken's completed one-minute candle near five minutes ago."""
    global kraken_5m_snapshot
    if kraken_last_quote is None or now - kraken_last_quote[0] > 30:
        return None
    try:
        response = http.get(KRAKEN_URL.replace("/Ticker", "/OHLC"),
                            params={"pair": "XBTUSD", "interval": 1}, timeout=10)
        response.raise_for_status()
        payload = response.json()
        if payload.get("error") or not payload.get("result"):
            raise ValueError("Kraken OHLC returned no data")
        result = payload["result"]
        key = next((name for name in result if name != "last"), None)
        candles = result.get(key, []) if key else []
        candles = [c for c in candles if len(c) > 4]
        if not candles:
            raise ValueError("Kraken OHLC contains no candles")
        target_time = now - 300
        # Compare with the close of the candle whose end is nearest to t-5m.
        prior = min(candles, key=lambda c: abs((float(c[0]) + 60) - target_time))
        prior_time = float(prior[0]) + 60
        current = kraken_last_quote[1]
        prior_price = dec(prior[4])
        if abs(prior_time - target_time) > 45 or prior_price is None:
            raise ValueError("No Kraken candle near the five-minute reference")
        kraken_5m_snapshot = (now, prior_price, current)
        if prior_price == current:
            return None
        return "UP" if current > prior_price else "DOWN"
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError, StopIteration):
        # Fall back to in-process samples if Kraken's OHLC endpoint is briefly unavailable.
        log.warning("Kraken five-minute candle unavailable; using recent samples")
        if not kraken_samples:
            return None
        target_time = now - 300
        prior_time, prior_price = min(kraken_samples, key=lambda sample: abs(sample[0] - target_time))
        current_time, current = kraken_samples[-1]
        if abs(prior_time - target_time) > 45 or current_time < now - 30 or prior_price == current:
            return None
        kraken_5m_snapshot = (now, prior_price, current)
        return "UP" if current > prior_price else "DOWN"

def kraken_opening_projection(now, remaining, opened_at=None):
    """Project from the earliest fresh BTC reference captured after this contract opened."""
    if kraken_last_quote is None or remaining < 0:
        return None
    quote_time, current = kraken_last_quote
    if now - quote_time > 30:
        return None
    samples = [
        (t, price) for t, price in kraken_samples
        if t <= now and (opened_at is None or t >= opened_at)
    ]
    if not samples:
        return None
    prior_time, prior = min(samples, key=lambda sample: sample[0])
    measured_seconds = Decimal(str(now - prior_time))
    if measured_seconds < Decimal("10") or measured_seconds > Decimal("90") or current == prior:
        return None
    change_since_open = current - prior
    horizon = min(max(Decimal("0"), Decimal(str(remaining))), Decimal("300"))
    projected_move = change_since_open * horizon / measured_seconds
    cap = current * Decimal("0.0025")
    projected_move = max(-cap, min(cap, projected_move))
    projected = current + projected_move
    if projected <= 0 or projected == current:
        return None
    return current, projected

def kraken_close_projection(now, remaining):
    """Project from Kraken's fresh five-minute reference, including across worker restarts."""
    global kraken_5m_snapshot
    if kraken_last_quote is None or remaining < 0:
        return None
    quote_time, current = kraken_last_quote
    if now - quote_time > 30:
        return None
    if kraken_5m_snapshot and now - kraken_5m_snapshot[0] <= 30:
        _, prior, _ = kraken_5m_snapshot
    elif kraken_samples:
        target_time = now - 300
        prior_time, prior = min(kraken_samples, key=lambda sample: abs(sample[0] - target_time))
        if abs(prior_time - target_time) > 45:
            return None
    else:
        return None
    if current == prior:
        return None
    change_5m = current - prior
    # Keep the forecast horizon aligned with a five-minute window.
    horizon = min(max(Decimal("0"), Decimal(str(remaining))), Decimal("300"))
    projected = current + change_5m * horizon / Decimal("300")
    if projected <= 0:
        return None
    return current, projected

def kraken_nearest_level_projection(now, direction, projection):
    """Cap an extrapolated target at the nearest nearby swing level."""
    if projection is None or kraken_last_quote is None:
        return projection
    quote_time, current = kraken_last_quote
    if now - quote_time > 30:
        return None
    try:
        response = http.get(KRAKEN_URL.replace("/Ticker", "/OHLC"),
                            params={"pair": "XBTUSD", "interval": 1}, timeout=10)
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result") or {}
        key = next((name for name in result if name != "last"), None)
        candles = result.get(key, [])[-20:] if key else []
        highs = [dec(candle[2]) for candle in candles if len(candle) > 4]
        lows = [dec(candle[3]) for candle in candles if len(candle) > 4]
        highs = [price for price in highs if price is not None]
        lows = [price for price in lows if price is not None]
        if direction == "UP":
            levels = [level for level in highs if level > current]
            level = min(levels) if levels else None
            # If price has already reached the recent ceiling, don't forecast
            # another leg upward from that resistance.
            recent_ceiling = max(highs) if highs else None
            ceiling_gap = recent_ceiling - current if recent_ceiling is not None else None
            if ceiling_gap is not None and ceiling_gap <= current * Decimal("0.0001"):
                log.info("Skipping UP projection: BTC has touched recent resistance %s", recent_ceiling)
                return current, current
        else:
            levels = [level for level in lows if level < current]
            level = max(levels) if levels else None
            recent_floor = min(lows) if lows else None
            floor_gap = current - recent_floor if recent_floor is not None else None
            if floor_gap is not None and floor_gap <= current * Decimal("0.0001"):
                log.info("Skipping DOWN projection: BTC has touched recent support %s", recent_floor)
                return current, current
        if level is not None and abs(level - current) >= current * Decimal("0.0002"):
            target = min(projection[1], level) if direction == "UP" else max(projection[1], level)
            if target != projection[1]:
                log.info("Capping %s target at nearest recent swing level: %s", direction, level)
            return current, target
        # No usable swing level: limit the linear projection so one recent
        # five-minute impulse cannot be extended without bound to the close.
        cap = current * Decimal("0.0025")
        projected_move = projection[1] - current
        if direction == "UP":
            target = current + min(max(projected_move, Decimal(0)), cap)
        else:
            target = current - min(max(-projected_move, Decimal(0)), cap)
        return current, target
    except (requests.RequestException, ValueError, KeyError, IndexError,
            TypeError, StopIteration):
        log.exception("Could not get nearby BTC swing levels; using conservative fallback")
        cap = current * Decimal("0.0025")
        move = projection[1] - current
        target = current + min(max(move, Decimal(0)), cap) if direction == "UP" else current - min(max(-move, Decimal(0)), cap)
        return current, target

def dec(x):
    try:
        v = Decimal(str(x))
        return v if v.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None

def ts(x):
    try:
        return datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp() if x else None
    except (ValueError, AttributeError):
        return None


def pattern_session_start(now):
    """Return the start of today's 06:00 New York pattern session."""
    local_now = datetime.fromtimestamp(now, ZoneInfo("America/New_York"))
    start = local_now.replace(hour=6, minute=0, second=0, microsecond=0)
    if local_now < start:
        start -= timedelta(days=1)
    return start.timestamp()

def db_open():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.execute("""CREATE TABLE IF NOT EXISTS telegram_state
               (key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
    # Telegram permits outbound messages only to chats that have opened the bot.
    # Store private chat IDs only; never store names, usernames, or question text.
    db.execute("""CREATE TABLE IF NOT EXISTS telegram_subscribers
               (chat_id TEXT PRIMARY KEY)""")
    subscriber_seed = db.execute(
        "SELECT value FROM telegram_state WHERE key='subscriber_seeded'").fetchone()
    if CHAT_ID and not subscriber_seed:
        db.execute("INSERT OR IGNORE INTO telegram_subscribers(chat_id) VALUES (?)",
                   (CHAT_ID,))
        db.execute("""INSERT INTO telegram_state(key,value)
                      VALUES ('subscriber_seeded','1')""")
    btc_assistant.init_access(db)
    db.execute("""CREATE TABLE IF NOT EXISTS kalshi_only_samples
               (ticker TEXT, observed_at REAL, yes_mid TEXT,
                PRIMARY KEY(ticker, observed_at))""")
    # Persist one official Kalshi outcome for each completed BTC 15-minute market.
    db.execute("""CREATE TABLE IF NOT EXISTS kalshi_15m_outcomes
               (close_time INTEGER PRIMARY KEY, ticker TEXT NOT NULL,
                result TEXT NOT NULL, direction TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS kalshi_15m_chart_sent
               (close_time INTEGER PRIMARY KEY)""")
    db.execute("""CREATE TABLE IF NOT EXISTS kalshi_only_alerts
               (ticker TEXT, phase TEXT, sent_at REAL, direction TEXT,
                PRIMARY KEY(ticker,phase))""")
    alert_columns = {row[1] for row in db.execute("PRAGMA table_info(kalshi_only_alerts)")}
    for column in ("bot_version", "rules_snapshot"):
        if column not in alert_columns:
            db.execute(f"ALTER TABLE kalshi_only_alerts ADD COLUMN {column} TEXT")
    db.execute("""CREATE TABLE IF NOT EXISTS kalshi_only_results
               (ticker TEXT PRIMARY KEY, result TEXT, observed_at REAL)""")
    # Durable forecast feedback: keep reported corrections and score each
    # sent BTC/USD projection against Kraken's one-minute close near settlement.
    db.execute("""CREATE TABLE IF NOT EXISTS kalshi_prediction_feedback
               (id TEXT PRIMARY KEY, ticker TEXT, phase TEXT,
                predicted_direction TEXT, actual_direction TEXT,
                issued_at REAL, close_at REAL, start_price TEXT,
                target_price TEXT, actual_price TEXT, evaluated_at REAL,
                status TEXT NOT NULL, source TEXT NOT NULL, note TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS adaptive_models
               (name TEXT PRIMARY KEY, model_json TEXT NOT NULL,
                trained_at REAL NOT NULL, samples INTEGER NOT NULL,
                validation_score REAL NOT NULL)""")
    # Upgrade persistent databases created by earlier bot versions.
    feedback_columns = {row[1] for row in db.execute(
        "PRAGMA table_info(kalshi_prediction_feedback)")}
    for column, declaration in (
            ("high_after_signal", "TEXT"),
            ("low_after_signal", "TEXT"),
            ("target_reached", "INTEGER"),
            ("model_features", "TEXT"),
            ("bot_version", "TEXT"),
            ("rules_snapshot", "TEXT")):
        if column not in feedback_columns:
            db.execute(f"ALTER TABLE kalshi_prediction_feedback ADD COLUMN {column} {declaration}")
    # Shadow journal: captures only the first quote observed during the
    # market's opening minute. It never sends alerts or places orders.
    db.execute("""CREATE TABLE IF NOT EXISTS kalshi_opening_journal
               (ticker TEXT PRIMARY KEY, observed_at REAL NOT NULL,
                close_at REAL NOT NULL, yes_bid TEXT NOT NULL,
                yes_ask TEXT NOT NULL, yes_mid TEXT NOT NULL,
                kraken_usd TEXT, outcome TEXT, settled_at REAL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS rafa_shadow
        (ticker TEXT, phase TEXT, issued REAL, session TEXT, direction TEXT,
         mid REAL, change REAL, spread REAL, volume REAL, kraken TEXT,
         btc REAL, volatility REAL, candidate TEXT, PRIMARY KEY(ticker,phase))""")
    db.execute("""CREATE TABLE IF NOT EXISTS rafa_shadow_daily
        (day TEXT PRIMARY KEY, report TEXT NOT NULL)""")
    reset_version = "learning-reset-2026-11-25-v2"
    reset_row = db.execute(
        "SELECT value FROM telegram_state WHERE key='learning_reset_version'").fetchone()
    if not reset_row or reset_row[0] != reset_version:
        reset_tables = (
            "kalshi_only_samples", "kalshi_15m_outcomes",
            "kalshi_15m_chart_sent", "kalshi_only_alerts",
            "kalshi_only_results", "kalshi_prediction_feedback",
            "adaptive_models", "kalshi_opening_journal", "rafa_shadow",
            "rafa_shadow_daily", "rafa_candle_context",
            "rafa_shadow_milestones", "rafa_close_estimates",
            "kalshi_wait_notices", "kalshi_combo_forecasts",
            "kalshi_combo_reviews",
        )
        for table in reset_tables:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()
            if exists:
                db.execute(f"DELETE FROM {table}")
        db.execute("DELETE FROM telegram_state WHERE key IN "
                   "('adaptive_training_state','dashboard_sync_attempt')")
        db.execute("""INSERT INTO telegram_state(key,value)
                      VALUES ('learning_reset_version',?)
                      ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                   (reset_version,))
        log.warning("Learning, model, and signal history cleared; Telegram access preserved")
    db.commit()
    return db

def get_json(path, params=None):
    for attempt in range(4):
        try:
            r = http.get(BASE + path, params=params, timeout=15)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(min(30, 2 ** (attempt + 1)))
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("Kalshi request failed: %s", exc)
            if attempt == 3:
                return None
            time.sleep(2 ** attempt)
    return None

def alerts_paused(now=None):
    """Scheduled notification pauses are disabled by user request."""
    return False


def notify(msg, db=None, broadcast=False, photo=None):
    if broadcast and alerts_paused():
        log.info("Broadcast notification suppressed until ALERTS_PAUSED_UNTIL")
        return False
    if not TOKEN:
        log.warning("Telegram not configured")
        return False
    if broadcast and db is not None:
        recipients = {str(row[0]) for row in db.execute(
            "SELECT chat_id FROM telegram_subscribers").fetchall()
            if btc_assistant.allowed(db, str(row[0]))}
    else:
        recipients = {btc_assistant.OWNER_ID} if btc_assistant.valid_private_id(btc_assistant.OWNER_ID) else set()
    recipients.discard("")
    if not recipients:
        log.warning("Telegram has no subscribed recipients")
        return False

    delivered = False
    for recipient in recipients:
        try:
            response = None
            if photo is not None:
                response = http.post(
                    "https://api.telegram.org/bot" + TOKEN + "/sendPhoto",
                    data={"chat_id": recipient, "caption": msg[:1024]},
                    files={"photo": ("rafa_signal.png", photo, "image/png")}, timeout=20)
                if response.status_code in (400, 413, 415):
                    response = None
            if response is None:
                response = http.post(
                    "https://api.telegram.org/bot" + TOKEN + "/sendMessage",
                    json={"chat_id": recipient, "text": msg[:4000]}, timeout=15)
            if response.ok and response.json().get("ok"):
                delivered = True
            elif broadcast and db is not None and response.status_code in (400, 403):
                # A user may have blocked the bot or deleted their Telegram account.
                db.execute("DELETE FROM telegram_subscribers WHERE chat_id=?",
                           (recipient,))
                log.info("Removed an unreachable Telegram subscriber")
            else:
                log.warning("Telegram delivery failed with HTTP %s", response.status_code)
        except (requests.RequestException, ValueError):
            log.exception("Telegram send failed")
    return delivered

def quote(m):
    bid, ask = dec(m.get("yes_bid_dollars")), dec(m.get("yes_ask_dollars"))
    if bid is None or ask is None or not (Decimal(0) < bid <= ask < Decimal(1)):
        return None
    return bid, ask, (bid + ask) / 2

def sample(db, ticker, now, mid):
    db.execute("INSERT OR IGNORE INTO kalshi_only_samples VALUES (?,?,?)",
               (ticker, now, str(mid)))

def recent_btc_streak(db, now):
    """Return the current signed BTC streak using outcomes from this 06:00 NY session."""
    session_start = pattern_session_start(now)
    try:
        rows = db.execute("""SELECT close_time,direction FROM kalshi_15m_outcomes
            WHERE close_time>=? AND close_time<=? ORDER BY close_time DESC LIMIT 16""",
            (int(session_start), int(now))).fetchall()
    except sqlite3.Error:
        log.exception("Recent BTC 15m streak unavailable")
        return 0.0, "Contexto de racha BTC 15m: datos no disponibles."
    if not rows:
        return 0.0, "Contexto de racha BTC 15m: reuniendo cierres oficiales desde las 6:00 a. m. NY."
    direction = rows[0][1]
    if direction not in ("UP", "DOWN"):
        return 0.0, "Contexto de racha BTC 15m: dato oficial inválido."
    streak = 1
    previous_close = rows[0][0]
    for close_time, item_direction in rows[1:]:
        if item_direction != direction or previous_close-close_time != 900:
            break
        streak += 1
        previous_close = close_time
    signed_streak = min(streak, 4) / 4.0
    if direction == "DOWN":
        signed_streak = -signed_streak
    return signed_streak, f"📚 Racha BTC 15m de hoy: {direction_label(direction)} × {streak} cierres consecutivos desde las 6:00 a. m. NY."


def btc_cycle_context(db, now):
    """Compare today's latest three BTC cycles only with matches from today's 06:00 NY session."""
    global btc_cycle_context_cache
    session_start = pattern_session_start(now)
    latest_row = db.execute("""SELECT MAX(close_time) FROM kalshi_15m_outcomes
        WHERE close_time>=? AND close_time<=?""",
        (int(session_start), int(now))).fetchone()
    latest_close = latest_row[0] if latest_row else None
    if (btc_cycle_context_cache is not None
            and btc_cycle_context_cache[0] == session_start
            and btc_cycle_context_cache[1] == latest_close
            and now-btc_cycle_context_cache[2] < 60):
        return btc_cycle_context_cache[3], btc_cycle_context_cache[4]

    rows = db.execute("""SELECT close_time,direction FROM kalshi_15m_outcomes
        WHERE close_time>=? AND close_time<=?
        ORDER BY close_time DESC LIMIT 600""",
        (int(session_start), int(now))).fetchall()
    rows.reverse()
    if len(rows) < 3:
        result = (None, "Patrón BTC 15m de hoy: reuniendo tres cierres oficiales desde las 6:00 a. m. NY.")
    else:
        times = [row[0] for row in rows]
        directions = [row[1] for row in rows]
        pattern = tuple(directions[-3:])
        pattern_text = " → ".join(pattern)
        if times[-1]-times[-2] != 900 or times[-2]-times[-3] != 900:
            result = (None, "Patrón BTC 15m de hoy: faltan cierres consecutivos para comparar.")
        else:
            following = []
            for index in range(3, len(directions)):
                if (times[index]-times[index-1] == 900
                        and times[index-1]-times[index-2] == 900
                        and times[index-2]-times[index-3] == 900
                        and tuple(directions[index-3:index]) == pattern):
                    following.append(directions[index])
            count = len(following)
            ups = sum(direction == "UP" for direction in following)
            downs = count-ups
            bias = None
            if count >= 12:
                posterior_up = (ups+1)/(count+2)
                if posterior_up >= 0.65:
                    bias = "UP"
                elif posterior_up <= 0.35:
                    bias = "DOWN"
            if count:
                summary = (f"Patrón de hoy {pattern_text}: después hubo UP {ups}/{count} "
                           f"y DOWN {downs}/{count} veces.")
            else:
                summary = f"Patrón de hoy {pattern_text}: todavía no hay casos comparables."
            if bias is None:
                summary += " Muestra insuficiente para inclinar la señal."
            else:
                summary += f" Sesgo de hoy: {direction_label(bias)}."
            result = (bias, summary)
    btc_cycle_context_cache = (session_start, latest_close, now, result[0], result[1])
    return result


def prepare_btc_opening_context(db, now):
    """Prepare streak and three-cycle context from outcomes since 06:00 New York time."""
    global btc_opening_context_cache
    session_start = pattern_session_start(now)
    latest_row = db.execute("""SELECT MAX(close_time) FROM kalshi_15m_outcomes
        WHERE close_time>=? AND close_time<=?""",
        (int(session_start), int(now))).fetchone()
    latest_close = latest_row[0] if latest_row else None
    if (btc_opening_context_cache is not None
            and btc_opening_context_cache[0] == session_start
            and btc_opening_context_cache[1] == latest_close
            and now-btc_opening_context_cache[2] < 60):
        return btc_opening_context_cache[3:]
    cycle_bias, cycle_summary = btc_cycle_context(db, now)
    btc_streak, streak_text = recent_btc_streak(db, now)
    btc_opening_context_cache = (
        session_start, latest_close, now, btc_streak, streak_text, cycle_bias, cycle_summary)
    log.debug("Prepared today's BTC opening context from 06:00 New York outcomes")
    return btc_opening_context_cache[3:]


def record_kalshi_15m_chart(db, now):
    """Store settled Kalshi BTC 15m outcomes and send their streak chart."""
    payload = get_json("/markets", {
        "series_ticker": SERIES, "status": "settled", "limit": 100
    })
    if payload is None:
        return
    for market in payload.get("markets", []):
        ticker = market.get("ticker", "")
        if not isinstance(ticker, str) or not ticker.startswith(SERIES + "-"):
            continue
        opened, closes = ts(market.get("open_time")), ts(market.get("close_time"))
        result = str(market.get("result") or "").lower()
        if opened is None or closes is None or closes > now:
            continue
        # Only retain 15-minute BTC contracts with an official yes/no settlement.
        if abs((closes - opened) - 900) > 60 or result not in ("yes", "no"):
            continue
        direction = "UP" if result == "yes" else "DOWN"
        db.execute("""INSERT OR IGNORE INTO kalshi_15m_outcomes
            (close_time, ticker, result, direction) VALUES (?,?,?,?)""",
            (int(closes), ticker, result, direction))

    latest = db.execute("""SELECT close_time, ticker, result, direction
                           FROM kalshi_15m_outcomes
                           ORDER BY close_time DESC LIMIT 1""").fetchone()
    if latest is None:
        return
    close_time, ticker, result, direction = latest
    if db.execute("SELECT 1 FROM kalshi_15m_chart_sent WHERE close_time=?",
                  (close_time,)).fetchone():
        return
    rows = db.execute("""SELECT close_time, direction
                         FROM kalshi_15m_outcomes
                         ORDER BY close_time DESC LIMIT 16""").fetchall()
    rows.reverse()
    last_four = rows[-4:]
    up_16 = sum(row[1] == "UP" for row in rows)
    down_16 = sum(row[1] == "DOWN" for row in rows)
    up_4 = sum(row[1] == "UP" for row in last_four)
    down_4 = sum(row[1] == "DOWN" for row in last_four)
    direction = rows[-1][1]
    markers = {"UP": "🟩", "DOWN": "🟥"}
    chart = "".join(markers[row[1]] for row in rows)
    streak = 0
    for row in reversed(rows):
        if row[1] != direction:
            break
        streak += 1
    arrow = direction_label(direction)
    close_label = datetime.fromtimestamp(close_time, timezone.utc).strftime("%H:%M UTC")
    message = (
        "📊 KALSHI BTC 15M · resultados oficiales\n"
        f"🟥 DOWN | 🟩 UP · antiguo → reciente\n{chart}\n"
        f"Últimas 4: DOWN {down_4} · UP {up_4}\n"
        f"Últimas 16: DOWN {down_16} · UP {up_16}\n"
        f"Racha actual: {arrow} × {streak}\n"
        f"Último cierre {close_label}: {result.upper()} · {ticker}"
    )
    if notify(message, db=db, broadcast=True):
        db.execute("INSERT OR IGNORE INTO kalshi_15m_chart_sent VALUES (?)",
                   (close_time,))

def record_settled_eth_results(db, now):
    """Keep recent official ETH 15-minute settlements for Kalshi-only cycle learning."""
    payload = get_json("/markets", {
        "series_ticker": ETH_SERIES, "status": "settled", "limit": 100
    })
    if payload is None:
        return
    for market in payload.get("markets", []):
        ticker = market.get("ticker", "")
        if not isinstance(ticker, str) or not ticker.startswith(ETH_SERIES + "-"):
            continue
        opened, closes = ts(market.get("open_time")), ts(market.get("close_time"))
        result = str(market.get("result") or "").lower()
        if (opened is None or closes is None or closes > now
                or abs((closes-opened)-900) > 60 or result not in ("yes","no")):
            continue
        db.execute("""INSERT OR IGNORE INTO kalshi_only_results
            (ticker,result,observed_at) VALUES (?,?,?)""",
            (ticker,result,closes))


def journal_opening_quote(db, market, now, open_at, close_at):
    """Record an opening-minute quote for later out-of-sample evaluation."""
    elapsed = now - open_at
    if elapsed < 0 or elapsed > 60:
        return
    q = quote(market)
    if q is None:
        return
    bid, ask, mid = q
    kraken_price = None
    if kraken_last_quote and now - kraken_last_quote[0] <= 30:
        kraken_price = str(kraken_last_quote[1])
    db.execute("""INSERT OR IGNORE INTO kalshi_opening_journal
        (ticker, observed_at, close_at, yes_bid, yes_ask, yes_mid, kraken_usd)
        VALUES (?,?,?,?,?,?,?)""",
        (market["ticker"], now, close_at, str(bid), str(ask), str(mid), kraken_price))

def make_model_features(phase, mid, kalshi_change, remaining, btc_streak):
    """Use Kalshi and historical features only; Kraken is excluded from direction."""
    return [
        1.0 if phase == "LAST_5M" else 0.0,
        float(mid),
        float(kalshi_change) * 10.0,
        max(0.0, min(1.0, float(remaining) / 900.0)),
        max(-1.0, min(1.0, float(btc_streak))),
    ]


def _sigmoid(value):
    value = max(-30.0, min(30.0, value))
    return 1.0 / (1.0 + math.exp(-value))


def train_adaptive_model(db, now):
    """Fit a small local classifier from settled, out-of-sample bot forecasts."""
    session_start = pattern_session_start(now)
    rows = db.execute("""SELECT model_features, actual_direction
        FROM kalshi_prediction_feedback
        WHERE status='evaluated' AND source='bot_forecast'
          AND model_features IS NOT NULL
          AND actual_direction IN ('UP','DOWN')
          AND issued_at>=? AND issued_at<?
        ORDER BY evaluated_at ASC""",
        (session_start, now)).fetchall()
    examples = []
    for raw_features, actual in rows:
        try:
            features = json.loads(raw_features)
            if len(features) == 5 and all(math.isfinite(float(v)) for v in features):
                examples.append(([float(v) for v in features], 1.0 if actual == "UP" else 0.0))
        except (TypeError, ValueError):
            continue
    if len(examples) < 50:
        log.info("Adaptive training waiting for 50 scored forecasts; currently %d", len(examples))
        return False
    split = int(len(examples) * 0.8)
    split = max(35, min(split, len(examples) - 15))
    training, validation = examples[:split], examples[split:]
    if (sum(y for _, y in training) < 8
            or len(training) - sum(y for _, y in training) < 8
            or sum(y == 1.0 for _, y in validation) < 5
            or sum(y == 0.0 for _, y in validation) < 5):
        log.info("Adaptive training waiting for balanced UP/DOWN examples")
        return False
    columns = list(zip(*(x for x, _ in training)))
    means = [sum(col) / len(col) for col in columns]
    scales = []
    for col, mean in zip(columns, means):
        variance = sum((value - mean) ** 2 for value in col) / len(col)
        scales.append(math.sqrt(variance) or 1.0)

    def normalize(features):
        return [(value - mean) / scale for value, mean, scale in zip(features, means, scales)]

    weights = [0.0] * (len(examples[0][0]) + 1)
    learning_rate = 0.04
    for _ in range(500):
        gradients = [0.0] * len(weights)
        for features, label in training:
            vector = [1.0] + normalize(features)
            error = _sigmoid(sum(w * x for w, x in zip(weights, vector))) - label
            for index, value in enumerate(vector):
                gradients[index] += error * value
        for index in range(len(weights)):
            gradient = gradients[index] / len(training)
            if index:
                gradient += 0.01 * weights[index]
            weights[index] -= learning_rate * gradient

    true_up = true_down = hit_up = hit_down = 0
    for features, label in validation:
        probability = _sigmoid(sum(w * x for w, x in zip(weights, [1.0] + normalize(features))))
        predicted_up = probability >= 0.5
        if label == 1.0:
            true_up += 1
            hit_up += int(predicted_up)
        else:
            true_down += 1
            hit_down += int(not predicted_up)
    balanced_accuracy = ((hit_up / true_up) + (hit_down / true_down)) / 2
    if balanced_accuracy < 0.58:
        log.info("Adaptive model kept previous version; validation balanced accuracy %.3f",
                 balanced_accuracy)
        return False
    payload = {
        "weights": weights, "means": means, "scales": scales,
        "balanced_accuracy": balanced_accuracy, "samples": len(examples),
        "validation_samples": len(validation),
        "pattern_session_start": session_start,
    }
    db.execute("""INSERT INTO adaptive_models(name, model_json, trained_at, samples, validation_score)
        VALUES ('btc_direction', ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET model_json=excluded.model_json,
          trained_at=excluded.trained_at, samples=excluded.samples,
          validation_score=excluded.validation_score""",
        (json.dumps(payload, separators=(",", ":")), now, len(examples), balanced_accuracy))
    log.info("Adaptive BTC model trained: n=%d, validation balanced accuracy=%.3f",
             len(examples), balanced_accuracy)
    return True


def adaptive_model_allows(db, features, direction):
    """Log model disagreements for learning without suppressing requested alerts."""
    if features is None:
        return True
    row = db.execute("SELECT model_json FROM adaptive_models WHERE name='btc_direction'").fetchone()
    if not row:
        return True
    try:
        model = json.loads(row[0])
        if len(model.get("means", [])) != len(features):
            return True
        if (model.get("samples", 0) < 100
                or model.get("validation_samples", 0) < 20
                or model.get("balanced_accuracy", 0) < 0.62):
            return True
        vector = [1.0] + [
            (value - mean) / scale
            for value, mean, scale in zip(features, model["means"], model["scales"])
        ]
        probability_up = _sigmoid(sum(w * x for w, x in zip(model["weights"], vector)))
        model_direction = "UP" if probability_up >= 0.5 else "DOWN"
        confidence = max(probability_up, 1.0 - probability_up)
        if model_direction != direction and confidence >= 0.65:
            log.info("Adaptive model disagrees: leans %s vs signal %s (%.1f%%); alert remains enabled",
                     model_direction, direction, confidence*100)
    except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError):
        log.exception("Adaptive model could not read its saved parameters")
    return True

def adaptive_model_prediction(db, features, now):
    """Use a BTC model only if it was trained from this 06:00 NY session's forecasts."""
    if features is None:
        return None
    row = db.execute("SELECT model_json FROM adaptive_models WHERE name='btc_direction'").fetchone()
    if not row:
        return None
    try:
        model = json.loads(row[0])
        if (model.get("pattern_session_start") != pattern_session_start(now)
                or len(model.get("means", [])) != len(features)
                or model.get("samples", 0) < 50
                or model.get("validation_samples", 0) < 10
                or model.get("balanced_accuracy", 0) < 0.58):
            return None
        vector = [1.0] + [
            (float(value)-mean)/scale
            for value, mean, scale in zip(features, model["means"], model["scales"])
        ]
        probability_up = _sigmoid(sum(w*x for w,x in zip(model["weights"], vector)))
        confidence = max(probability_up, 1.0-probability_up)
        if confidence < 0.55:
            return None
        return ("UP" if probability_up >= 0.5 else "DOWN",
                probability_up, int(model["samples"]),
                float(model["balanced_accuracy"]))
    except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError):
        log.exception("Adaptive direction model could not be evaluated")
        return None


def cycle_pattern_summary(db, asset, now):
    """Summarize only completed Kalshi cycles and samples from today's 06:00 NY session."""
    session_start = pattern_session_start(now)
    if asset == "BTC":
        markets = db.execute("""SELECT ticker,direction FROM kalshi_15m_outcomes
            WHERE close_time>=? AND close_time<=? AND direction IN ('UP','DOWN')
            ORDER BY close_time DESC LIMIT 20""",
            (int(session_start), int(now))).fetchall()
    else:
        markets = db.execute("""SELECT ticker,CASE WHEN result='yes' THEN 'UP' ELSE 'DOWN' END
            FROM kalshi_only_results WHERE ticker LIKE ? AND observed_at>=?
              AND observed_at<=? AND result IN ('yes','no')
            ORDER BY observed_at DESC LIMIT 20""",
            (ETH_SERIES + "-%", session_start, now)).fetchall()
    completed = len(markets)
    up_closes = sum(direction == "UP" for _, direction in markets)
    down_closes = completed - up_closes
    above_seconds = below_seconds = flips = observed_cycles = 0.0
    if markets:
        tickers = [ticker for ticker, _ in markets]
        placeholders = ",".join("?" for _ in tickers)
        samples = db.execute(
            f"SELECT ticker,observed_at,yes_mid FROM kalshi_only_samples "
            f"WHERE ticker IN ({placeholders}) AND observed_at>=? AND observed_at<=? "
            f"ORDER BY ticker,observed_at",
            (*tickers, session_start, now)).fetchall()
        by_ticker = {}
        for ticker, observed, raw_mid in samples:
            mid = dec(raw_mid)
            if mid is not None:
                by_ticker.setdefault(ticker, []).append((float(observed), mid))
        for ticker in tickers:
            points = by_ticker.get(ticker, [])
            if len(points) < 2:
                continue
            observed_cycles += 1
            for (time_a, mid_a), (time_b, mid_b) in zip(points, points[1:]):
                elapsed = max(0.0, time_b-time_a)
                if mid_a >= Decimal("0.50"):
                    above_seconds += elapsed
                    if mid_b < Decimal("0.50"):
                        flips += 1
                else:
                    below_seconds += elapsed
                    if mid_b >= Decimal("0.50"):
                        flips += 1
    if observed_cycles:
        above_avg = above_seconds/60/observed_cycles
        below_avg = below_seconds/60/observed_cycles
        flip_avg = flips/observed_cycles
        quote_text = (f"Kalshi YES >50¢ {above_avg:.1f} min/ciclo · "
                      f"<50¢ {below_avg:.1f} min/ciclo · {flip_avg:.1f} giros/ciclo")
    else:
        quote_text = "duración y giros intracontrato: reuniendo muestras"
    return {
        "markets": markets, "completed": completed, "up": up_closes,
        "down": down_closes,
        "summary": (f"Ciclos de hoy desde 6:00 a. m. NY n={completed}: "
                    f"UP {up_closes}/DOWN {down_closes}; {quote_text}."),
    }


def combo_directional_bias(db, asset, now):
    """Compare today's settled combo legs only, preserving older results in storage."""
    prefix = "btc" if asset == "BTC" else "eth"
    ticker_column = prefix + "_ticker"
    direction_column = prefix + "_direction"
    session_start = pattern_session_start(now)
    rows = db.execute(f"""SELECT c.{direction_column},
            CASE WHEN r.result='yes' THEN 'UP' ELSE 'DOWN' END
        FROM kalshi_combo_forecasts c
        JOIN kalshi_only_results r ON r.ticker=c.{ticker_column}
        WHERE r.result IN ('yes','no') AND c.alert_sent=1 AND c.issued_at>=? AND c.issued_at<=?
        ORDER BY c.issued_at DESC LIMIT 100""",
        (session_start, now)).fetchall()
    rates = {}
    for direction in ("UP", "DOWN"):
        samples = [actual == direction for predicted, actual in rows
                   if predicted == direction]
        if len(samples) >= 5:
            rates[direction] = (sum(samples)+1)/(len(samples)+2)
    if "UP" not in rates or "DOWN" not in rates:
        return 0.0
    return max(-1.0, min(1.0, rates["UP"]-rates["DOWN"]))


def learned_direction(db, market, asset, now, phase):
    """Combine completed Kalshi cycles with this contract's live Kalshi quote movement."""
    q = quote(market)
    if q is None:
        return None
    _, _, mid = q
    close_at = ts(market.get("close_time")) or now+900
    opened_at = ts(market.get("open_time"))
    previous_context = ""
    previous_direction = None
    opening_path_score = 0.0
    if phase == "EARLY_5M" and asset == "BTC" and opened_at is not None:
        previous = db.execute("""SELECT ticker,direction,close_time FROM kalshi_15m_outcomes
            WHERE close_time BETWEEN ? AND ? AND direction IN ('UP','DOWN')
            ORDER BY close_time DESC LIMIT 1""",
            (int(opened_at)-5, int(opened_at)+5)).fetchone()
        if previous is None:
            log.info("Opening forecast waiting for the previous official BTC settlement: %s",
                     market.get("ticker", ""))
            return None
        previous_ticker, previous_direction, previous_close = previous
        prior_path = db.execute("""SELECT yes_mid FROM kalshi_only_samples
            WHERE ticker=? ORDER BY observed_at ASC""", (previous_ticker,)).fetchall()
        path_values = [dec(row[0]) for row in prior_path]
        path_values = [value for value in path_values if value is not None]
        if len(path_values) < 2:
            log.info("Opening forecast waiting for full Kalshi YES path of previous contract %s",
                     previous_ticker)
            return None
        quote_change = float(path_values[-1] - path_values[0])
        above_share = sum(value >= Decimal("0.50") for value in path_values) / len(path_values)
        opening_path_score = max(-1.0, min(1.0,
            0.55 * max(-1.0, min(1.0, quote_change / 0.05))
            + 0.45 * (2.0 * above_share - 1.0)))
        crossings = sum((left >= Decimal("0.50")) != (right >= Decimal("0.50"))
                        for left, right in zip(path_values, path_values[1:]))
        previous_context = (
            f"Contrato anterior {previous_ticker}: resultado oficial {previous_direction}; "
            f"YES {path_values[0]:.3f}→{path_values[-1]:.3f}, "
            f"muestras={len(path_values)}, cruces de 50¢={crossings}."
        )
    else:
        prior_quote = db.execute("""SELECT yes_mid FROM kalshi_only_samples
            WHERE ticker=? AND observed_at<=? ORDER BY observed_at DESC LIMIT 1""",
            (market.get("ticker", ""), now-60)).fetchone()
        prior_mid = dec(prior_quote[0]) if prior_quote else None
        quote_change = float(mid-prior_mid) if prior_mid is not None else 0.0
    cycle = cycle_pattern_summary(db, asset, now)
    outcomes = [direction for _, direction in cycle["markets"]]
    history_score = ((cycle["up"]-cycle["down"])/cycle["completed"]
                     if cycle["completed"] else 0.0)
    live_score = max(-1.0, min(1.0, quote_change/0.05))
    combo_bias = combo_directional_bias(db, asset, now) if phase == "COMBO_5M" else 0.0
    if phase == "EARLY_5M" and previous_direction in ("UP", "DOWN"):
        previous_score = 1.0 if previous_direction == "UP" else -1.0
        combined_score = 0.50*previous_score + 0.25*opening_path_score + 0.25*history_score
    else:
        combined_score = 0.55*live_score + 0.35*history_score + 0.10*combo_bias
    if combined_score > 0:
        direction = "UP"
    elif combined_score < 0:
        direction = "DOWN"
    elif previous_direction in ("UP", "DOWN"):
        direction = previous_direction
    elif outcomes:
        direction = outcomes[0]
    else:
        direction = "UP" if mid >= Decimal("0.50") else "DOWN"
    btc_streak, _, _, cycle_summary = prepare_btc_opening_context(db, now)
    feature_phase = "LAST_5M" if phase in ("LAST_5M", "COMBO_5M") else "EARLY_5M"
    features = make_model_features(feature_phase, mid, quote_change,
                                   close_at-now, btc_streak)
    if asset == "BTC":
        model_hint = adaptive_model_prediction(db, features, now)
        if model_hint is not None:
            model_direction, model_probability_up, model_samples, model_score = model_hint
            model_confidence = max(model_probability_up, 1.0-model_probability_up)
            if (model_samples >= 100 and model_score >= 0.62
                    and model_confidence >= 0.65):
                if model_direction != direction:
                    log.info(
                        "Validated adaptive model selected %s over Kalshi cycle direction %s "
                        "(confidence %.1f%%, balanced validation %.1f%%, n=%d)",
                        model_direction, direction, model_confidence*100,
                        model_score*100, model_samples)
                direction = model_direction
            else:
                log.info(
                    "Adaptive model remains advisory: confidence %.1f%%, "
                    "balanced validation %.1f%%, n=%d",
                    model_confidence*100, model_score*100, model_samples)
    basis = (f"Kalshi actual + histórico n={cycle['completed']} "
             f"(UP {cycle['up']}/DOWN {cycle['down']}) + movimiento YES {quote_change:+.3f}")
    return (direction, features, basis, quote_change,
            f"{previous_context} {cycle['summary']} {cycle_summary}".strip())


def maybe_train_adaptive_model(db, now):
    """Run the daily self-learning pass at 9 a.m. New York time, retrying on new results."""
    local_now = datetime.fromtimestamp(now, ZoneInfo("America/New_York"))
    if (local_now.hour, local_now.minute) < (9, 0):
        return
    today = local_now.strftime("%Y-%m-%d")
    session_start = pattern_session_start(now)
    count = db.execute("""SELECT COUNT(*) FROM kalshi_prediction_feedback
        WHERE status='evaluated' AND source='bot_forecast' AND model_features IS NOT NULL
          AND actual_direction IN ('UP','DOWN')
          AND issued_at>=? AND issued_at<?""",
        (session_start, now)).fetchone()[0]
    state_key = f"adaptive_training_state_6am_{int(session_start)}"
    row = db.execute("SELECT value FROM telegram_state WHERE key=?", (state_key,)).fetchone()
    if row and row[0].startswith(today + ":"):
        _, status, previous_count = row[0].split(":")
        if status == "trained" or count < int(previous_count) + 5:
            return
    log.info("Daily self-learning pass at %s: reviewing %d settled bot forecasts",
             local_now.strftime("%Y-%m-%d %H:%M %Z"), count)
    trained = train_adaptive_model(db, now)
    state = f"{today}:{'trained' if trained else 'pending'}:{count}"
    db.execute("""INSERT INTO telegram_state(key,value) VALUES (?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (state_key, state))


def adaptive_direction_gate(db, phase, direction, asset="BTC"):
    """Measure recent accuracy for learning, but never pause an alert automatically."""
    if phase in ("COMBO_5M", "COMBO_6M"):
        table = db.execute("""SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='kalshi_combo_forecasts'""").fetchone()
        if not table:
            return True
        prefix = "btc" if asset == "BTC" else "eth"
        ticker_column = prefix + "_ticker"
        direction_column = prefix + "_direction"
        rows = db.execute(f"""SELECT c.{direction_column},
                CASE WHEN r.result='yes' THEN 'UP' ELSE 'DOWN' END
            FROM kalshi_combo_forecasts c
            JOIN kalshi_only_results r ON r.ticker=c.{ticker_column}
            WHERE r.result IN ('yes','no') AND c.alert_sent=1
            ORDER BY r.observed_at DESC LIMIT 12""").fetchall()
    else:
        rows = db.execute("""SELECT predicted_direction, actual_direction
            FROM kalshi_prediction_feedback
            WHERE status='evaluated' AND source='bot_forecast' AND phase=?
              AND predicted_direction IN ('UP','DOWN') AND actual_direction IN ('UP','DOWN')
            ORDER BY evaluated_at DESC LIMIT 12""", (phase,)).fetchall()
    direction_rows = [(predicted, actual) for predicted, actual in rows
                      if predicted == direction][:8]
    if len(direction_rows) >= 4:
        hits = sum(actual == direction for _, actual in direction_rows)
        accuracy = hits / len(direction_rows)
        log.info("Learning context %s %s %s accuracy %.0f%% (%d/%d); alert remains enabled",
                 phase, asset, direction, accuracy * 100, hits, len(direction_rows))
    if len(rows) >= 4:
        hits = sum(predicted == actual for predicted, actual in rows)
        log.info("Learning context %s overall accuracy %.0f%% (%d/%d); alert remains enabled",
                 phase, 100*hits/len(rows), hits, len(rows))
    return True

def rafa_candle_context(db, ticker, phase, now):
    # Closed Kraken OHLC candles only; never use the unfinished candle.
    db.execute("""CREATE TABLE IF NOT EXISTS rafa_candle_context
        (ticker TEXT, phase TEXT, issued REAL, features TEXT,
         PRIMARY KEY(ticker,phase))""")
    features = {}
    for interval in (1, 5, 15):
        try:
            payload = requests.get("https://api.kraken.com/0/public/OHLC",
                params={"pair":"XBTUSD","interval":interval}, timeout=4).json()
            result = payload.get("result", {})
            candles = next((v for k,v in result.items() if k != "last"), [])
            closed = [x for x in candles if float(x[0])+interval*60 <= now]
            if len(closed) < 6:
                continue
            recent = closed[-6:]
            first, last = float(recent[0][4]), float(recent[-1][4])
            if first <= 0 or last <= 0:
                continue
            features[str(interval)] = {
                "return": last/first-1,
                "last_body": (last-float(recent[-1][1]))/last,
                "range": (max(float(x[2]) for x in recent)-min(float(x[3]) for x in recent))/last,
                "volume": float(recent[-1][6]),
                "closed_at": float(recent[-1][0])+interval*60}
        except Exception:
            log.warning("Rafa candle data unavailable for %s minutes", interval)
    db.execute("INSERT OR IGNORE INTO rafa_candle_context VALUES (?,?,?,?)",
               (ticker,phase,now,json.dumps(features)))
    log.info("Rafa candle context %s %s intervals=%s",ticker,phase,list(features))

def shadow_record(db, ticker, phase, now, direction, q, change, volume, kraken_direction):
    hour = time.gmtime(now).tm_hour
    session = "Asia" if hour < 7 else "Europe" if hour < 13 else "US" if hour < 22 else "Asia"
    btc = float(kraken_last_quote[1]) if kraken_last_quote and now-kraken_last_quote[0] < 60 else None
    prices = [float(p) for t,p in kraken_samples if now-t <= 300]
    volatility = (max(prices)-min(prices))/prices[0] if len(prices)>1 and prices[0]>0 else None
    # Learn only from outcomes already known at prediction time.
    rows = db.execute("""SELECT s.direction,s.kraken,r.result FROM rafa_shadow s
        JOIN kalshi_only_results r ON r.ticker=s.ticker
        WHERE s.session=? AND s.phase=? AND r.observed_at<? AND s.issued<?
        ORDER BY s.issued DESC LIMIT 300""",(session,phase,now,now)).fetchall()
    comparable = [(d,k,y) for d,k,y in rows if k in ("UP","DOWN") and d!=k]
    candidate = direction
    if len(comparable)>=50 and kraken_direction in ("UP","DOWN"):
        wins = sum((k=="UP")== (y=="yes") for d,k,y in comparable)
        if wins/len(comparable)>=0.60:
            candidate=kraken_direction
    db.execute("""INSERT OR IGNORE INTO rafa_shadow VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ticker,phase,now,session,direction,float(q[2]),float(change),float(q[1]-q[0]),
         float(volume),kraken_direction,btc,volatility,candidate))
    log.info("Rafa shadow recorded %s %s session=%s candidate=%s",ticker,phase,session,candidate)
    rafa_candle_context(db, ticker, phase, now)

def shadow_milestones(db, now):
    db.execute("""CREATE TABLE IF NOT EXISTS rafa_shadow_milestones
        (phase TEXT, batch INTEGER, report TEXT, PRIMARY KEY(phase,batch))""")
    for phase in ("EARLY_5M", "LAST_5M"):
        rows=db.execute("""SELECT s.direction,s.candidate,r.result FROM rafa_shadow s
            JOIN kalshi_only_results r ON r.ticker=s.ticker
            WHERE (s.phase=? OR (s.phase='LATE_5M' AND ?='LAST_5M')) AND r.observed_at<? AND r.observed_at>=s.issued
            AND r.result IN ('yes','no') ORDER BY r.observed_at,s.ticker""",
            (phase,phase,now)).fetchall()
        for batch in range(len(rows)//50):
            if db.execute("SELECT 1 FROM rafa_shadow_milestones WHERE phase=? AND batch=?",
                          (phase,batch)).fetchone():
                continue
            block=rows[batch*50:(batch+1)*50]
            baseline=sum((d=="UP")== (y=="yes") for d,k,y in block)
            candidate=sum((k=="UP")== (y=="yes") for d,k,y in block)
            report={"n":50,"baseline_correct":baseline,"candidate_correct":candidate,
                    "baseline_pct":baseline*2,"candidate_pct":candidate*2,
                    "goal_80":candidate>=40,"goal_90":candidate>=45,
                    "goal_95":candidate>=48,"shadow_only":True}
            db.execute("INSERT INTO rafa_shadow_milestones VALUES (?,?,?)",
                       (phase,batch,json.dumps(report)))
            log.info("Rafa performance milestone %s batch %s: %s",phase,batch+1,report)

def shadow_daily(db, now):
    try:
        shadow_milestones(db, now)
    except Exception:
        log.exception("Rafa performance milestone failed")
    day=time.strftime("%Y-%m-%d",time.gmtime(now))
    if db.execute("SELECT 1 FROM rafa_shadow_daily WHERE day=?",(day,)).fetchone():
        return
    rows=db.execute("""SELECT s.session,s.phase,COUNT(*),
        SUM((s.direction='UP')=(r.result='yes')),
        SUM((s.candidate='UP')=(r.result='yes'))
        FROM rafa_shadow s JOIN kalshi_only_results r ON r.ticker=s.ticker
        WHERE r.observed_at<? GROUP BY s.session,s.phase""",(now,)).fetchall()
    db.execute("INSERT INTO rafa_shadow_daily VALUES (?,?)",(day,json.dumps(rows)))
    log.info("Rafa shadow daily evaluation %s: %s",day,rows)

def rafa_context(db, ticker, phase, now, direction, q, volume, kraken_direction):
    # Historical agreement is descriptive, not a calibrated probability.
    rows = db.execute("""SELECT s.direction,r.result FROM rafa_shadow s
        JOIN kalshi_only_results r ON r.ticker=s.ticker
        WHERE s.phase=? AND s.direction=? AND r.observed_at<?
        ORDER BY s.issued DESC LIMIT 100""",(phase,direction,now)).fetchall()
    n = len(rows)
    rate = sum((d=="UP")== (y=="yes") for d,y in rows)/n if n else None
    confidence = "sin historial suficiente" if n<30 else f"acierto histórico de cierre {rate:.0%} (n={n})"
    prices = [float(p) for t,p in kraken_samples if now-300<=t<=now]
    span = (max(prices)-min(prices))/prices[0]*100 if len(prices)>1 and prices[0]>0 else None
    volatility = f"{span:.3f}%" if span is not None else "N/D"
    return f"📊 {confidence}; rango BTC 5m {volatility}; spread YES {q[1]-q[0]:.3f}; volumen 24h {volume}."

def target_close_bias(now, remaining, start, target, projected, samples):
    """Require >80% directional momentum evidence before coloring the target bias."""
    recent = sorted((t, p) for t, p in samples if now-120 <= t <= now)
    if len(recent) < 4 or remaining <= 0 or start <= 0:
        return None, None
    duration = recent[-1][0] - recent[0][0]
    if duration < 60 or now-recent[-1][0] > 30 or recent[-1][1] != start:
        return None, None
    if any(b[0]-a[0] > 60 for a, b in zip(recent, recent[1:])):
        return None, None
    prices = [p for _, p in recent]
    move = prices[-1] - prices[0]
    if not move:
        return None, None
    changes = [b-a for a, b in zip(prices, prices[1:]) if a != b]
    if len(changes) < 3:
        return None, None
    aligned = sum(c*move > 0 for c in changes)
    evidence = aligned / len(changes)
    if evidence <= 0.80:
        return None, evidence
    noise = max(start*Decimal('0.0001'), (max(prices)-min(prices))/Decimal(2))
    if abs(move) < noise:
        return None, evidence
    short = start + move*Decimal(str(remaining))/Decimal(str(duration))
    blended = (short + projected)/Decimal(2)
    if short <= 0 or blended <= 0:
        return None, evidence
    if move > 0 and min(short, blended) > target+noise:
        return "UP", evidence
    if move < 0 and max(short, blended) < target-noise:
        return "DOWN", evidence
    return None, evidence



def direction_label(direction):
    return {"UP": "🟢 UP ↑", "DOWN": "🔴 DOWN ↓"}.get(direction, direction)


def forecast_history_line(db, phase, direction):
    """Show comparable settled forecast performance without presenting it as probability."""
    rows = db.execute("""SELECT predicted_direction=actual_direction
        FROM kalshi_prediction_feedback
        WHERE status='evaluated' AND source='bot_forecast'
          AND phase=? AND predicted_direction=?
          AND actual_direction IN ('UP','DOWN')
        ORDER BY evaluated_at DESC LIMIT 100""", (phase, direction)).fetchall()
    count = len(rows)
    if count < 30:
        return f"📊 Confianza: reuniendo datos ({count}/30)"
    correct = sum(bool(row[0]) for row in rows)
    return f"📊 Acierto histórico: {correct / count:.0%} (n={count})"


def closing_confidence(db, ticker, phase, direction, now, remaining, start, projected):
    """Per-forecast confidence estimate from comparable settled projection errors.
    Display only; not a calibrated probability or an additional prediction gate."""
    try:
        if start <= 0 or projected <= 0 or projected == start:
            return None
        rows = db.execute("""SELECT f.ticker, f.start_price, f.actual_price, e.estimated
            FROM kalshi_prediction_feedback f JOIN rafa_close_estimates e ON e.id=f.id
            WHERE f.status='evaluated' AND f.source='bot_forecast'
              AND f.phase=? AND f.predicted_direction=?
              AND f.evaluated_at<? AND f.close_at<=? AND f.ticker<>?
              AND f.bot_version=? AND f.rules_snapshot=?
              AND ABS((f.close_at-f.issued_at)-?)<=120
            ORDER BY f.evaluated_at DESC LIMIT 300""",
            (phase, direction, now, now, ticker, BOT_VERSION,
             "Kraken BTC/USD separate first target and close estimate", remaining)).fetchall()
        move = float((projected-start)/start)
        seen = set()
        successes = total = 0
        for prior_ticker, old_start, actual, estimate in rows:
            if not prior_ticker or prior_ticker in seen:
                continue
            base, observed, expected = dec(old_start), dec(actual), dec(estimate)
            if any(v is None or not v.is_finite() or v <= 0 for v in (base, observed, expected)):
                continue
            error = float((observed-expected)/base)
            if not math.isfinite(error):
                continue
            seen.add(prior_ticker)
            total += 1
            successes += int(move+error > 0 if direction == "UP" else move+error < 0)
        if total < 30:
            return None
        return min(99, int(100*(successes+1)/(total+2)))
    except (sqlite3.Error, ArithmeticError, ValueError, TypeError):
        log.exception("Rafa display confidence unavailable")
        return None


def forecast_display(ticker, phase_label, direction, confidence, start, target, projected,
                     bias_direction, bias_evidence, cycle_text):
    """Format a BTC alert; Kraken is a numeric reference, never a direction input."""
    if bias_direction == "UP":
        bias_text = "🔄 Posible giro en el objetivo: 🟢 UP · sesgo de cierre por encima del objetivo."
    elif bias_direction == "DOWN":
        bias_text = "🔄 Posible giro en el objetivo: 🔴 DOWN · sesgo de cierre por debajo del objetivo."
    else:
        bias_text = "🔄 Posible giro en el objetivo: 🟡 no confirmado · evidencia insuficiente."
    if start is not None and target is not None and projected is not None:
        price_text = (
            f"🌐 Referencia numérica Kraken BTC/USD: USD {start:,.2f}\n"
            f"🎯 Primer objetivo: USD {target:,.2f}\n"
            f"📍 Cierre estimado por momentum: USD {projected:,.2f}\n"
        )
    else:
        price_text = (
            f"🌐 Referencia numérica Kraken BTC/USD: "
            f"{'USD '+format(start, ',.2f') if start is not None else 'no disponible'}\n"
            "🎯 Proyección numérica: no disponible en esta apertura.\n"
        )
    message = (
        f"{direction_label(direction)} · {phase_label}\n"
        f"🧾 {ticker}\n"
        f"{price_text}"
        f"{bias_text}\n"
        f"{cycle_text}\n"
        "La dirección usa Kalshi y el resultado/recorrido del contrato anterior.\n"
        "Proyección experimental, no garantía."
    )
    return message, None

def send_early_wait_notice(db, m, now):
    """Tell subscribers after the opening review window if direction is not confirmed."""
    ticker = m.get("ticker", "")
    opened = ts(m.get("open_time"))
    if not ticker or opened is None or not (OPENING_REVIEW_SECONDS <= now-opened <= 300):
        return
    db.execute("""CREATE TABLE IF NOT EXISTS kalshi_wait_notices
        (ticker TEXT, phase TEXT, sent_at REAL, PRIMARY KEY(ticker,phase))""")
    if db.execute("""SELECT 1 FROM kalshi_only_alerts
                     WHERE ticker=? AND phase='EARLY_5M' LIMIT 1""",
                  (ticker,)).fetchone():
        return
    if db.execute("""SELECT 1 FROM kalshi_wait_notices
                     WHERE ticker=? AND phase='EARLY_5M' LIMIT 1""",
                  (ticker,)).fetchone():
        return
    message = (
        "🟡 BTC · PRIMEROS 5 MIN\n"
        f"🧾 {ticker}\n"
        "⏳ Sin confirmación todavía.\n"
        f"Ya revisé los primeros {OPENING_REVIEW_SECONDS} segundos junto con los cierres oficiales anteriores. "
        "Aún no hay confirmación suficiente de UP o DOWN; sigo evaluando y enviaré la señal si coincide la evidencia dentro de esta ventana."
    )
    if notify(message, db=db, broadcast=True):
        db.execute("INSERT OR IGNORE INTO kalshi_wait_notices (ticker,phase,sent_at) VALUES (?,?,?)",
                   (ticker, "EARLY_5M", now))
        log.info("Early forecast waiting notice sent: %s", ticker)


def learned_signal(db, market, now):
    """Send one BTC forecast from prior model results and collected history."""
    ticker = market.get("ticker", "")
    if not ticker or not ticker.startswith(SERIES + "-"):
        return
    if db.execute("SELECT 1 FROM kalshi_only_alerts WHERE ticker=? LIMIT 1",
                  (ticker,)).fetchone():
        return
    q = quote(market)
    if q is None:
        return
    bid, ask, mid = q
    close_at = ts(market.get("close_time"))
    if close_at is None or close_at <= now:
        return
    direction, features, basis, quote_change, cycle_summary = learned_direction(
        db, market, "BTC", now, "EARLY_5M")
    if direction not in ("UP", "DOWN"):
        return
    entry = ask if direction == "UP" else Decimal(1)-bid
    remaining = max(0, int((close_at-now)/60))
    message = (
        "🚨 KALSHI BTC 15 MIN · APRENDIZAJE\n\n"
        f"📊 SEÑAL: {direction_label(direction)}\n"
        f"💰 Entrada indicativa: USD {entry:.2f}\n"
        f"📈 Base: {basis}\n"
        f"📊 Kalshi YES actual: {mid:.0%} · cambio recopilado: {quote_change:+.4f}\n"
        f"⏱ Tiempo restante: {remaining} min\n"
        f"🎯 Contrato: {ticker}\n"
        "El aprendizaje usa resultados oficiales e historial; no garantiza el cierre."
    )
    if not notify(message, db=db, broadcast=True):
        return
    db.execute("""INSERT OR IGNORE INTO kalshi_only_alerts
        (ticker,phase,sent_at,direction,bot_version,rules_snapshot)
        VALUES (?,?,?,?,?,?)""",
        (ticker, "LEARNED", now, direction, BOT_VERSION, RULES_SNAPSHOT))
    db.execute("""INSERT OR IGNORE INTO kalshi_prediction_feedback
        (id,ticker,phase,predicted_direction,issued_at,close_at,status,source,
         bot_version,rules_snapshot,model_features)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (f"kalshi-learned:{ticker}", ticker, "LEARNED", direction, now, close_at,
         "pending", "bot_forecast", BOT_VERSION, RULES_SNAPSHOT,
         json.dumps(features, separators=(",", ":")) if features is not None else None))
    try:
        shadow_record(db, ticker, "EARLY_5M", now, direction, q,
                      Decimal(str(quote_change)), dec(market.get("volume_24h_fp")) or Decimal(0),
                      None)
    except Exception:
        log.exception("Learning shadow record failed")
    log.info("Learned BTC15M alert sent: %s %s; source=%s",
             ticker, direction, basis)


def signal(db, m, now, phase, kraken_direction=None):
    """Issue opening or final-five-minute BTC forecasts; Kalshi sets direction."""
    ticker = m["ticker"]
    alert_key = (ticker, phase)
    if alert_key in alerted_phases:
        return
    if db.execute("SELECT 1 FROM kalshi_only_alerts WHERE ticker=? AND phase=? LIMIT 1",
                  (ticker, phase)).fetchone():
        alerted_phases.add(alert_key)
        return
    q = quote(m)
    if q is None:
        return
    _, _, mid = q
    volume = dec(m.get("volume_24h_fp"))
    if volume is None:
        volume = dec(m.get("volume_24h"))
    volume = volume or Decimal(0)
    close_at = ts(m.get("close_time"))
    opened_at = ts(m.get("open_time"))
    if close_at is None or opened_at is None or close_at <= now:
        return
    remaining = close_at-now
    elapsed = now-opened_at
    if phase == "EARLY_5M":
        if not (OPENING_REVIEW_SECONDS <= elapsed <= 300):
            return
    elif phase == "LAST_5M":
        if not (0 < remaining <= 300):
            return
    else:
        return
    forecast = learned_direction(db, m, "BTC", now, phase)
    if forecast is None:
        return
    direction, _, history_basis, quote_change, cycle_summary = forecast
    if phase == "EARLY_5M":
        # Direction comes from the previous official Kalshi result and its full YES path.
        # Kraken contributes numeric BTC/USD projection only; it cannot set direction.
        projection = kraken_close_projection(now, remaining)
        phase_label = "APERTURA · CONTRATO ANTERIOR COMPLETO"
        if projection is None:
            start = (kraken_last_quote[1] if kraken_last_quote
                     and now-kraken_last_quote[0] <= 30 else None)
            projected = first_target = None
            log.info("Opening direction remains eligible without Kraken numeric projection: %s",
                     ticker)
        else:
            start, raw_projected = projection
            move = abs(raw_projected-start)
            if move <= 0:
                projected = first_target = None
            else:
                projected = start + move if direction == "UP" else start-move
                first_projection = kraken_nearest_level_projection(
                    now, direction, (start, projected))
                if first_projection is None or first_projection[1] == start:
                    first_projection = (start, projected)
                first_target = first_projection[1]
    else:
        projection = kraken_close_projection(now, remaining)
        if projection is None:
            log.info("Skipping %s final-five forecast: BTC price reference unavailable",
                     ticker)
            return
        start, raw_projected = projection
        move = abs(raw_projected-start)
        if move <= 0:
            return
        projected = start + move if direction == "UP" else start-move
        phase_label = "ÚLTIMOS 5 MIN"
        first_projection = kraken_nearest_level_projection(now, direction, (start, projected))
        if first_projection is None or first_projection[1] == start:
            first_projection = (start, projected)
        first_target = first_projection[1]
    btc_streak, streak_text, _, _ = prepare_btc_opening_context(db, now)
    if phase == "EARLY_5M":
        # Reuse the previous contract's full Kalshi YES path for model feedback.
        kalshi_change = quote_change
    else:
        prior_quote = db.execute("""SELECT yes_mid FROM kalshi_only_samples
            WHERE ticker=? AND observed_at<=? ORDER BY observed_at DESC LIMIT 1""",
            (ticker, now-60)).fetchone()
        prior_mid = dec(prior_quote[0]) if prior_quote else None
        kalshi_change = float(mid-prior_mid) if prior_mid is not None else 0.0
    model_features = make_model_features(phase, mid, kalshi_change,
                                         remaining, btc_streak)
    cycle_text = f"{streak_text}\n{cycle_summary}\nBase: {history_basis}"
    # Kraken supplies BTC price levels only; it does not vote on direction or reversal.
    bias, bias_evidence = None, "La dirección se calcula con cierres y cotización de Kalshi."
    db.execute("""CREATE TABLE IF NOT EXISTS rafa_close_estimates
        (id TEXT PRIMARY KEY, estimated TEXT, absolute_error TEXT)""")
    confidence = (closing_confidence(
        db, ticker, phase, direction, now, remaining, start, projected)
        if start is not None and projected is not None else None)
    message, photo = forecast_display(
        ticker, phase_label, direction, confidence, start, first_target,
        projected, bias, bias_evidence, cycle_text)
    if notify(message, db=db, broadcast=True, photo=photo):
        alerted_phases.add(alert_key)
        db.execute("""INSERT OR IGNORE INTO kalshi_only_alerts
            (ticker,phase,sent_at,direction,bot_version,rules_snapshot)
            VALUES (?,?,?,?,?,?)""",
            (ticker, phase, now, direction, BOT_VERSION, RULES_SNAPSHOT))
        db.execute("""INSERT OR IGNORE INTO kalshi_prediction_feedback
            (id,ticker,phase,predicted_direction,issued_at,close_at,start_price,
             target_price,status,source,bot_version,rules_snapshot,model_features)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"kalshi-cycle:{ticker}:{phase}",ticker,phase,direction,now,close_at,
             str(start) if start is not None else None,
             str(first_target) if first_target is not None else None,
             "pending","bot_forecast",BOT_VERSION,RULES_SNAPSHOT,
             json.dumps(model_features, separators=(",", ":"))
             if model_features is not None else None))
        db.execute("INSERT OR IGNORE INTO rafa_close_estimates (id,estimated) VALUES (?,?)",
                   (f"kalshi-cycle:{ticker}:{phase}",
                    str(projected) if projected is not None else None))
        try:
            shadow_record(db, ticker, phase, now, direction, q, Decimal(str(kalshi_change)),
                          volume, None)
        except Exception:
            log.exception("Rafa shadow recording failed")
        log.info("Kalshi %s forecast: %s %s; BTC price reference from Kraken only",
                 phase, ticker, direction)

def evaluate_btc_forecasts(db, now):
    """Score forecast direction and whether its first target was touched before expiry."""
    rows = db.execute("""SELECT id, ticker, phase, predicted_direction, close_at,
                                start_price, target_price, issued_at
                         FROM kalshi_prediction_feedback
                         WHERE status='pending' AND close_at <= ?
                           AND phase IN ('EARLY_5M','LAST_5M')
                         ORDER BY close_at LIMIT 100""", (now,)).fetchall()
    for row in rows:
        (forecast_id, ticker, phase, predicted, close_at, start_text,
         target_text, issued_at) = row
        try:
            response = http.get(
                "https://api.kraken.com/0/public/OHLC",
                params={"pair": "XBTUSD", "interval": 1,
                        "since": max(0, int(issued_at) - 60)},
                timeout=10)
            response.raise_for_status()
            payload = response.json()
            if payload.get("error"):
                continue
            series = payload.get("result", {})
            key = next((k for k in series if k != "last"), None)
            if key is None:
                continue
            candles = series.get(key, [])
            window = [c for c in candles if len(c) > 6
                      and float(c[0]) + 60 <= close_at + 1
                      and float(c[0]) >= issued_at]
            if not window:
                continue
            close_candle = min(window, key=lambda c: abs(float(c[0]) + 60 - close_at))
            if abs(float(close_candle[0]) + 60 - close_at) > 1:
                continue
            actual_price = dec(close_candle[4])
            start_price = dec(start_text)
            target_price = dec(target_text)
            highs = [dec(c[2]) for c in window]
            lows = [dec(c[3]) for c in window]
            highs = [v for v in highs if v is not None]
            lows = [v for v in lows if v is not None]
            if actual_price is None or start_price is None or not highs or not lows:
                continue
            peak = max(highs)
            trough = min(lows)
            actual = ("UP" if actual_price > start_price else
                      "DOWN" if actual_price < start_price else "FLAT")
            correct = predicted == actual
            reached = None
            if target_price is not None:
                reached = peak >= target_price if predicted == "UP" else trough <= target_price
            if reached and actual != predicted:
                note = "First target was touched, then price reversed before settlement."
            elif reached:
                note = "First target was touched; settlement direction also matched."
            elif actual == predicted:
                note = "Settlement direction matched; first target was not observed."
            else:
                note = "Direction missed and first target was not observed."
            db.execute("""UPDATE kalshi_prediction_feedback
                SET actual_direction=COALESCE(actual_direction,?), actual_price=?, evaluated_at=?,
                    high_after_signal=?, low_after_signal=?, target_reached=?,
                    status='evaluated', note=?
                WHERE id=? AND status='pending'""",
                (actual, str(actual_price), now, str(peak), str(trough),
                 int(reached) if reached is not None else None,
                 note, forecast_id))
            db.execute("""CREATE TABLE IF NOT EXISTS rafa_close_estimates
                (id TEXT PRIMARY KEY, estimated TEXT, absolute_error TEXT)""")
            estimate_row = db.execute("SELECT estimated FROM rafa_close_estimates WHERE id=?", (forecast_id,)).fetchone()
            estimate = dec(estimate_row[0]) if estimate_row else None
            estimate_line = ""
            if estimate is not None:
                error = abs(actual_price-estimate)
                db.execute("UPDATE rafa_close_estimates SET absolute_error=? WHERE id=?", (str(error),forecast_id))
                estimate_line = f"📐 Cierre estimado: USD {estimate:,.2f}; error: USD {error:,.2f}\n".replace("\n", "\n")
            target_line = ("Primer objetivo: tocado" if reached else
                           "Primer objetivo: no observado" if reached is False else
                           "Primer objetivo: sin dato")
            reversal_line = ("🔄 El precio tocó el objetivo y luego revirtió antes del cierre.\n"
                             if reached and actual != predicted else "")
            notify("🏁 RESULTADO OFICIAL — EVALUACIÓN BTC\n"
                   f"🧾 {ticker} ({phase})\n"
                   f"🔮 Pronóstico: {direction_label(predicted)} desde USD {start_price:,.2f}\n"
                   f"📈 Máximo/mínimo después de la señal: USD {peak:,.2f} / USD {trough:,.2f}\n"
                   f"🎯 {target_line} ({'USD ' + format(target_price, ',.2f') if target_price is not None else 'sin dato'})\n"
                   f"📍 Cierre aproximado: {direction_label(actual)} en USD {actual_price:,.2f}\n"
                   + estimate_line
                   + reversal_line
                   + ("✅ Dirección al cierre coincidió." if correct else
                      "❌ Dirección al cierre falló; datos guardados."),
                   db=db, broadcast=True)
        except (requests.RequestException, ValueError, KeyError, IndexError,
                TypeError, StopIteration):
            log.exception("Could not evaluate BTC forecast for %s", ticker)


def ensure_combo_forecast_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS kalshi_combo_forecasts
        (combo_key TEXT PRIMARY KEY, btc_ticker TEXT NOT NULL, eth_ticker TEXT NOT NULL,
         issued_at REAL NOT NULL, close_at REAL NOT NULL,
         btc_direction TEXT NOT NULL, eth_direction TEXT NOT NULL,
         alert_sent INTEGER NOT NULL DEFAULT 1,
         combo_version TEXT NOT NULL DEFAULT 'legacy')""")
    columns = {row[1] for row in db.execute(
        "PRAGMA table_info(kalshi_combo_forecasts)")}
    if "alert_sent" not in columns:
        # Existing rows are real alerts; rejected candidates are stored as shadow rows.
        db.execute("""ALTER TABLE kalshi_combo_forecasts
            ADD COLUMN alert_sent INTEGER NOT NULL DEFAULT 1""")
    if "combo_version" not in columns:
        db.execute("""ALTER TABLE kalshi_combo_forecasts
            ADD COLUMN combo_version TEXT NOT NULL DEFAULT 'legacy'""")

def evaluate_combo_reviews(db, now):
    ensure_combo_forecast_table(db)
    db.execute("""CREATE TABLE IF NOT EXISTS kalshi_combo_reviews
        (combo_key TEXT PRIMARY KEY, reviewed_at REAL NOT NULL)""")
    pending = db.execute("""SELECT c.combo_key,c.btc_ticker,c.eth_ticker,
            c.btc_direction,c.eth_direction,b.result,e.result,c.alert_sent
        FROM kalshi_combo_forecasts c
        JOIN kalshi_only_results b ON b.ticker=c.btc_ticker
        JOIN kalshi_only_results e ON e.ticker=c.eth_ticker
        LEFT JOIN kalshi_combo_reviews r ON r.combo_key=c.combo_key
        WHERE r.combo_key IS NULL AND b.result IN ('yes','no') AND e.result IN ('yes','no')
        ORDER BY c.close_at,c.combo_key""").fetchall()
    for combo_key, btc_ticker, eth_ticker, btc_pred, eth_pred, btc_result, eth_result, alert_sent in pending:
        btc_actual = "UP" if btc_result == "yes" else "DOWN"
        eth_actual = "UP" if eth_result == "yes" else "DOWN"
        db.execute("""UPDATE kalshi_prediction_feedback
            SET actual_direction=?, evaluated_at=?, status='evaluated',
                note='BTC combo leg scored against Kalshi official settlement.'
            WHERE id=? AND status='pending'""",
            (btc_actual, now, f"kalshi-combo-btc:{combo_key}"))
        btc_ok = btc_pred == btc_actual
        eth_ok = eth_pred == eth_actual
        combo_ok = btc_ok and eth_ok
        if not alert_sent:
            db.execute("INSERT OR IGNORE INTO kalshi_combo_reviews VALUES (?,?)",
                       (combo_key, now))
            log.info("Shadow combo evaluated without alert: %s; joint_success=%s",
                     combo_key, combo_ok)
            continue
        evaluated = db.execute("""SELECT c.btc_direction,c.eth_direction,b.result,e.result
            FROM kalshi_combo_forecasts c
            JOIN kalshi_only_results b ON b.ticker=c.btc_ticker
            JOIN kalshi_only_results e ON e.ticker=c.eth_ticker
            WHERE c.alert_sent=1 AND b.result IN ('yes','no') AND e.result IN ('yes','no')""").fetchall()
        wins = sum((bd == "UP") == (br == "yes") and
                   (ed == "UP") == (er == "yes")
                   for bd, ed, br, er in evaluated)
        total = len(evaluated)
        losses = total-wins
        accuracy = wins/total if total else 0.0
        message = "\n".join([
            "🏁 RESULTADO OFICIAL DEL COMBO BTC + ETH",
            f"🧾 BTC {btc_ticker}: pronóstico {direction_label(btc_pred)} · oficial {btc_result.upper()} {'✅' if btc_ok else '❌'}",
            f"🧾 ETH {eth_ticker}: pronóstico {direction_label(eth_pred)} · oficial {eth_result.upper()} {'✅' if eth_ok else '❌'}",
            f"🏁 Combo: {'✅ ACERTÓ LAS DOS PATAS' if combo_ok else '❌ NO ACERTÓ EL COMBO'}",
            f"📊 Acumulado: {wins} acertados · {losses} fallados de {total} ({accuracy:.0%} de combos completos).",
        ])
        if alerts_paused():
            db.execute("INSERT OR IGNORE INTO kalshi_combo_reviews VALUES (?,?)",
                       (combo_key, now))
            log.info("Combo result evaluated and saved during notification pause: %s",
                     combo_key)
        elif notify(message, db=db, broadcast=True):
            db.execute("INSERT OR IGNORE INTO kalshi_combo_reviews VALUES (?,?)",
                       (combo_key, now))


def check_results(db, now):
    # Include shadow combo candidates so they can be calibrated against official settlements.
    ensure_combo_forecast_table(db)
    # Use Kalshi's official result field, never Kraken, for completed markets.
    rows = db.execute("""SELECT ticker FROM (
            SELECT ticker FROM kalshi_only_alerts
            UNION SELECT btc_ticker AS ticker FROM kalshi_combo_forecasts
            UNION SELECT eth_ticker AS ticker FROM kalshi_combo_forecasts
        ) WHERE ticker NOT IN (SELECT ticker FROM kalshi_only_results)""").fetchall()
    for (ticker,) in rows[-30:]:
        payload = get_json("/markets/" + ticker)
        market = payload.get("market", {}) if payload else {}
        result = str(market.get("result") or "").lower()
        if result not in ("yes", "no"):
            continue
        db.execute("INSERT OR IGNORE INTO kalshi_only_results VALUES (?,?,?)",
                   (ticker, result, now))
        predictions = db.execute("""SELECT phase, direction FROM kalshi_only_alerts
            WHERE ticker=?""", (ticker,)).fetchall()
        for phase, direction in predictions:
            if phase in ("COMBO_5M", "COMBO_6M"):
                continue
            actual_direction = "UP" if result == "yes" else "DOWN"
            if phase in ("DAY1", "LEARNED"):
                feedback_id = f"kalshi-day1:{ticker}"
                db.execute("""UPDATE kalshi_prediction_feedback
                    SET actual_direction=?, evaluated_at=?, status='evaluated',
                        note='Scored against Kalshi official settlement.'
                    WHERE id=? AND status='pending'""",
                    (actual_direction, now, feedback_id))
            elif phase in ("EARLY_5M", "LAST_5M"):
                feedback_id = f"kalshi-cycle:{ticker}:{phase}"
                feedback = db.execute(
                    "SELECT start_price FROM kalshi_prediction_feedback WHERE id=?",
                    (feedback_id,)).fetchone()
                if feedback:
                    final_status = "evaluated" if feedback[0] is None else "pending"
                    db.execute("""UPDATE kalshi_prediction_feedback
                        SET actual_direction=?, evaluated_at=?, status=?,
                            note='Direction scored against Kalshi official settlement.'
                        WHERE id=? AND status='pending'""",
                        (actual_direction, now, final_status, feedback_id))
            correct = (direction == "UP" and result == "yes") or (
                direction == "DOWN" and result == "no")
            notify(f"🧠 BTC — REVISIÓN DE PREDICCIÓN\n🧾 {ticker}\n"
                   f"🔮 Pronóstico Kalshi: {direction_label(direction)}\n"
                   f"🏁 Resultado oficial: {result.upper()}\n"
                   f"{'✅ Coincidió' if correct else '❌ No coincidió'} con el resultado oficial.",
                   db=db, broadcast=True)

    evaluate_combo_reviews(db, now)

    # Price-based BTC reviews are secondary and must not hold up official results.
    evaluate_btc_forecasts(db, now)

    # Label every journaled opening quote with Kalshi's official settlement.
    # The market midpoint is a benchmark probability, not a bot forecast.
    pending = db.execute("""SELECT ticker FROM kalshi_opening_journal
        WHERE outcome IS NULL AND close_at <= ? ORDER BY close_at LIMIT 100""",
        (now,)).fetchall()
    for (ticker,) in pending:
        payload = get_json("/markets/" + ticker)
        market = payload.get("market", {}) if payload else {}
        result = str(market.get("result") or "").lower()
        if result in ("yes", "no"):
            db.execute("""UPDATE kalshi_opening_journal
                SET outcome=?, settled_at=? WHERE ticker=? AND outcome IS NULL""",
                (result, now, ticker))

    scored = db.execute("""SELECT yes_mid, outcome FROM kalshi_opening_journal
        WHERE outcome IN ('yes','no')""").fetchall()
    if scored:
        brier = sum((float(mid) - (1.0 if result == "yes" else 0.0)) ** 2
                    for mid, result in scored) / len(scored)
        log.info("Opening-minute Kalshi midpoint benchmark: n=%d Brier=%.4f; "
                 "not a Rafa probability forecast", len(scored), brier)


def _dashboard_time(value):
    return datetime.fromtimestamp(float(value), timezone.utc).isoformat() if value else None

def dashboard_snapshot(db, now):
    signals = []
    alerts = db.execute("""SELECT ticker,phase,sent_at,direction,bot_version,rules_snapshot
                           FROM kalshi_only_alerts ORDER BY sent_at DESC LIMIT 2000""").fetchall()
    for ticker, phase, sent_at, direction, version, rules in alerts:
        samples = db.execute("""SELECT observed_at,yes_mid FROM kalshi_only_samples
                                WHERE ticker=? ORDER BY observed_at ASC""", (ticker,)).fetchall()
        prior = next(((float(t), dec(price)) for t, price in samples
                      if float(t) <= float(sent_at) - 60 and dec(price) is not None), None)
        at_signal = next(((float(t), dec(price)) for t, price in reversed(samples)
                          if float(t) <= float(sent_at) + 1 and dec(price) is not None), None)
        outcome = db.execute("""SELECT result,close_time FROM kalshi_15m_outcomes
                                WHERE ticker=? ORDER BY close_time DESC LIMIT 1""", (ticker,)).fetchone()
        separate_result = db.execute("SELECT result FROM kalshi_only_results WHERE ticker=?", (ticker,)).fetchone()
        official = (outcome[0] if outcome else separate_result[0] if separate_result else "")
        close_at = float(outcome[1]) if outcome and outcome[1] else None
        observed = None
        actual = ""
        status = "pending"
        if close_at is not None and prior:
            close_sample = next(((float(t), dec(price)) for t, price in reversed(samples)
                                 if float(t) <= close_at and float(t) >= close_at - 90
                                 and dec(price) is not None), None)
            if close_sample:
                observed = close_sample[1]
                if observed > prior[1]:
                    actual = "UP"
                elif observed < prior[1]:
                    actual = "DOWN"
                else:
                    actual = "FLAT"
                status = "evaluated"
        initial = prior[1] if prior else None
        midpoint = at_signal[1] if at_signal else None
        target = min(Decimal("0.99"), max(Decimal("0.01"), midpoint + (midpoint - initial))) if midpoint is not None and initial is not None else None
        try:
            rule_text = json.dumps(json.loads(rules), ensure_ascii=False, sort_keys=True) if rules else ""
        except (TypeError, ValueError):
            rule_text = rules or ""
        signals.append({
            "id": f"kalshi:{ticker}:{phase}", "time": _dashboard_time(sent_at),
            "contract": ticker, "direction": direction, "basis": "Kalshi",
            "priceUnit": "Kalshi YES", "precision": 3,
            "start": str(initial) if initial is not None else "",
            "target": str(target) if target is not None else "",
            "observed": str(observed) if observed is not None else "",
            "actual": actual, "official": str(official or "").upper(),
            "officialCorrect": ((direction == "UP") == (str(official).lower() == "yes")) if str(official).lower() in ("yes","no") else None,
            "quoteDirectionCorrect": (direction == actual) if actual else None,
            "version": version or "No registrada en el historial",
            "rules": rule_text or "Reglas no guardadas en este registro",
            "tag": "Momentum de la cotización YES; resultado oficial conservado por separado.",
            "status": status, "phase": phase, "origin": "bot",
        })

    feedback = db.execute("""SELECT id,ticker,phase,predicted_direction,actual_direction,
            issued_at,start_price,target_price,actual_price,evaluated_at,status,source,note,
            high_after_signal,low_after_signal,target_reached,bot_version,rules_snapshot
            FROM kalshi_prediction_feedback ORDER BY COALESCE(issued_at,evaluated_at,rowid) DESC
            LIMIT 2000""").fetchall()
    for row in feedback:
        (record_id,ticker,phase,predicted,actual_direction,issued_at,start_price,
         target_price,actual_price,evaluated_at,status,source,note,high,low,reached,
         version,rules) = row
        evaluated = status == "evaluated"
        signals.append({
            "id": f"btc-feedback:{record_id}", "time": _dashboard_time(issued_at or evaluated_at),
            "contract": ticker or "BTC/USD · revisión", "direction": predicted or "",
            "basis": "BTC/USD" if source == "bot_forecast" else "Reporte manual",
            "priceUnit": "BTC/USD", "precision": 2,
            "start": start_price or "", "target": target_price or "",
            "observed": actual_price or "",
            "actual": actual_direction if evaluated and actual_direction in ("UP","DOWN","FLAT") else "",
            "reportedActual": actual_direction if status == "reported" else "",
            "official": "", "btcDirectionCorrect": (predicted == actual_direction) if evaluated and actual_direction in ("UP","DOWN","FLAT") else None,
            "targetReached": bool(reached) if reached in (0,1) else None, "version": version or "No registrada en el historial",
            "rules": rules or (f"Fuente: {source}" if source else "Reglas no registradas"),
            "tag": " · ".join(part for part in [
                note or "",
                f"Máx. {high} / mín. {low}" if high or low else "",
                "Objetivo tocado" if reached == 1 else "Objetivo no observado" if reached == 0 else "",
            ] if part),
            "status": status, "phase": phase or "", "origin": "bot",
        })

    versions = [{
        "id": f"runtime:{BOT_VERSION}", "name": BOT_VERSION,
        "date": _dashboard_time(now), "rules": RULES_SNAPSHOT,
        "note": "Versión y reglas observadas por el proceso de Rafa al sincronizar.",
        "origin": "bot",
    }]
    model = db.execute("SELECT trained_at,samples,validation_score FROM adaptive_models WHERE name='btc_direction'").fetchone()
    if model:
        versions.append({
            "id": "adaptive-model:btc_direction", "name": "Modelo adaptativo BTC",
            "date": _dashboard_time(model[0]),
            "rules": f"Muestras: {model[1]} · validación balanceada: {float(model[2]):.3f}",
            "note": "Metadatos del modelo entrenado en el bot.", "origin": "bot",
        })
    signals.sort(key=lambda item: item.get("time") or "", reverse=True)
    return {"source": "rafa-bot", "generated_at": _dashboard_time(now),
            "signals": signals, "versions": versions}

def sync_dashboard(db, now, force=False):
    if not PANEL_URL or not PANEL_SECRET or not PANEL_SITES_AUTH:
        return
    previous = db.execute("SELECT value FROM telegram_state WHERE key='dashboard_sync_attempt'").fetchone()
    if not force and previous and now - float(previous[0]) < 60:
        return
    db.execute("""INSERT INTO telegram_state(key,value) VALUES ('dashboard_sync_attempt',?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (str(now),))
    db.commit()
    try:
        payload = dashboard_snapshot(db, now)
        response = http.post(
            PANEL_URL + "/api/sync", json=payload,
            headers={
                "Authorization": "Bearer " + PANEL_SECRET,
                "OAI-Sites-Authorization": "Bearer " + PANEL_SITES_AUTH,
            },
            timeout=15,
        )
        if response.status_code < 200 or response.status_code >= 300:
            log.warning("Prediction panel sync returned HTTP %s", response.status_code)
            return
        log.info("Prediction panel sync completed: %d records", len(payload["signals"]))
    except (requests.RequestException, ValueError, sqlite3.Error):
        log.exception("Prediction panel sync failed")


def combo_history_summary(db, now, btc_direction, eth_direction):
    """Report smoothed leg rates and exact-pair joint outcomes."""
    rows = db.execute("""SELECT c.btc_direction,c.eth_direction,b.result,e.result
        FROM kalshi_combo_forecasts c
        JOIN kalshi_only_results b ON b.ticker=c.btc_ticker
        JOIN kalshi_only_results e ON e.ticker=c.eth_ticker
        WHERE c.issued_at<? AND c.combo_version=?
          AND b.result IN ('yes','no') AND e.result IN ('yes','no')
        ORDER BY c.issued_at DESC LIMIT 100""",
        (now, COMBO_RULE_VERSION)).fetchall()
    evaluated = []
    for btc_pred, eth_pred, btc_result, eth_result in rows:
        btc_actual = "UP" if btc_result == "yes" else "DOWN"
        eth_actual = "UP" if eth_result == "yes" else "DOWN"
        evaluated.append((
            btc_pred, eth_pred,
            btc_pred == btc_actual,
            eth_pred == eth_actual,
        ))

    def line(label, samples):
        if not samples:
            return f"{label}: reuniendo historial (n=0)"
        hits = sum(samples)
        n = len(samples)
        # Laplace smoothing avoids reporting 100% from one or two lucky outcomes.
        probability = (hits+1)/(n+2)
        quality = "preliminar" if n < 10 else "histórica"
        return f"{label}: {probability:.0%} {quality} ({hits}/{n}; n={n})"

    btc_samples = [btc_ok for bd, ed, btc_ok, eth_ok in evaluated
                   if bd == btc_direction]
    eth_samples = [eth_ok for bd, ed, btc_ok, eth_ok in evaluated
                   if ed == eth_direction]
    pair_samples = [btc_ok and eth_ok for bd, ed, btc_ok, eth_ok in evaluated
                    if bd == btc_direction and ed == eth_direction]
    global_samples = [btc_ok and eth_ok for _, _, btc_ok, eth_ok in evaluated]
    lines = [
        "📊 Historial validado del mismo algoritmo (incluye candidatos en sombra):",
        line(f"BTC {btc_direction}", btc_samples),
        line(f"ETH {eth_direction}", eth_samples),
    ]
    pair_label = f"Ambas ({btc_direction}+{eth_direction})"
    if len(pair_samples) >= 5:
        lines.append(line(pair_label, pair_samples))
    elif pair_samples:
        lines.append(line(pair_label, pair_samples) + " · muestra baja")
    elif global_samples:
        lines.append(
            f"{pair_label}: pocos casos exactos (n=0); "
            + line("base conjunta de todos los combos", global_samples))
    else:
        lines.append(f"{pair_label}: reuniendo historial (n=0)")
    return "\n".join(lines), sum(pair_samples), len(pair_samples)


def market_already_prices_direction_at_99(yes_mid, direction, remaining):
    """Only suppress a late forecast when Kalshi already prices its direction near 99%."""
    if remaining > 240:
        return False
    if direction == "UP":
        return yes_mid >= Decimal("0.99")
    if direction == "DOWN":
        return yes_mid <= Decimal("0.01")
    return False


def combo_leg(db, market, now, asset):
    """Choose direction from Kalshi history and current chart; Kraken never votes."""
    q = quote(market)
    if q is None:
        return None
    close_at = ts(market.get("close_time"))
    opened = ts(market.get("open_time"))
    if close_at is None or opened is None or close_at <= now or abs((close_at-opened)-900) > 60:
        return None
    forecast = learned_direction(db, market, asset, now, "COMBO_5M")
    if forecast is None:
        return None
    direction, features, basis, quote_change, pattern_summary = forecast
    horizon_seconds = min(max(0.0, close_at-now), 300.0)
    if horizon_seconds <= 0:
        return None
    if asset == "BTC":
        # Combo evaluation is Kalshi-only during this experiment; do not use Kraken.
        start = target = projected = move_amount = eta_seconds = None
        symbol = "BTC/USD · Kraken reference paused"
        price_unit = "USD"
        volume = dec(market.get("volume_24h_fp")) or dec(market.get("volume_24h")) or Decimal(0)
    else:
        # No Kraken ETH feed is used. The ETH leg shows Kalshi YES price movement.
        mid = q[2]
        start = mid
        previous = db.execute("""SELECT yes_mid,observed_at FROM kalshi_only_samples
            WHERE ticker=? AND observed_at<=? ORDER BY observed_at DESC LIMIT 1""",
            (market["ticker"], now-300)).fetchone()
        previous_mid = dec(previous[0]) if previous else None
        previous_time = float(previous[1]) if previous else None
        if (previous_mid is None or previous_time is None
                or now-previous_time > 360 or mid == previous_mid):
            return None
        move_5m = abs(mid-previous_mid)
        horizon = Decimal(str(horizon_seconds))/Decimal("300")
        projected = mid + (move_5m*horizon if direction == "UP" else -move_5m*horizon)
        projected = min(Decimal("0.99"), max(Decimal("0.01"), projected))
        first_projection = (start, projected)
        symbol = "ETH · Kalshi YES"
        price_unit = "YES"
        volume = dec(market.get("volume_24h_fp")) or dec(market.get("volume_24h")) or Decimal(0)
        quantum = Decimal("0.001")
        start = start.quantize(quantum)
        projected = projected.quantize(quantum)
        first_target = first_projection[1].quantize(quantum)
        if first_target == start and projected != start:
            first_target = projected
        projected_move = abs(projected-start)
        target_move = abs(first_target-start)
        if projected_move == 0 or target_move == 0:
            return None
        eta_seconds = horizon_seconds * min(1.0, float(target_move/projected_move))
        target = first_target
        move_amount = target_move
    btc_streak, _, _, _ = prepare_btc_opening_context(db, now)
    return {
        "asset": asset, "ticker": market["ticker"], "direction": direction,
        "start": start, "target": target, "projected": projected,
        "move_amount": move_amount, "eta_seconds": eta_seconds,
        "projection_horizon_seconds": horizon_seconds, "yes_mid": q[2],
        "volume": volume, "symbol": symbol, "price_unit": price_unit,
        "model_features": features, "learning_basis": basis,
        "cycle_summary": pattern_summary,
    }


def record_combo_candidate(db, combo_key, btc_leg, eth_leg, now, close_at, alert_sent):
    db.execute("""INSERT OR IGNORE INTO kalshi_combo_forecasts
        (combo_key,btc_ticker,eth_ticker,issued_at,close_at,
         btc_direction,eth_direction,alert_sent,combo_version)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (combo_key, btc_leg["ticker"], eth_leg["ticker"], now, close_at,
         btc_leg["direction"], eth_leg["direction"], int(alert_sent),
         COMBO_RULE_VERSION))


def send_btc_eth_combo(db, btc_market, eth_market, now):
    btc_close, eth_close = ts(btc_market.get("close_time")), ts(eth_market.get("close_time"))
    if btc_close is None or eth_close is None or abs(btc_close-eth_close) > 2:
        return
    remaining = btc_close-now
    if not (0 < remaining <= min(COMBO_WINDOW_SECONDS, 300)):
        return
    btc_leg = combo_leg(db, btc_market, now, "BTC")
    eth_leg = combo_leg(db, eth_market, now, "ETH")
    if btc_leg is None or eth_leg is None:
        return
    combo_key = btc_leg["ticker"] + "|" + eth_leg["ticker"]
    ensure_combo_forecast_table(db)
    if db.execute("SELECT 1 FROM kalshi_combo_forecasts WHERE combo_key=?",
                  (combo_key,)).fetchone():
        return
    btc_market_probability = (btc_leg["yes_mid"] if btc_leg["direction"] == "UP"
                              else Decimal("1")-btc_leg["yes_mid"])
    eth_market_probability = (eth_leg["yes_mid"] if eth_leg["direction"] == "UP"
                              else Decimal("1")-eth_leg["yes_mid"])
    if (COMBO_QUALITY_FILTERS_ENABLED and
            (btc_market_probability < COMBO_MIN_MARKET_PROBABILITY
             or eth_market_probability < COMBO_MIN_MARKET_PROBABILITY)):
        record_combo_candidate(db, combo_key, btc_leg, eth_leg, now, btc_close, False)
        log.info(
            "Combo shadowed: Kalshi odds disagree with direction or are below %.0f%% "
            "(BTC=%.1f%%, ETH=%.1f%%)",
            float(COMBO_MIN_MARKET_PROBABILITY*100),
            float(btc_market_probability*100), float(eth_market_probability*100))
        return
    # Calibrate only against candidates from this exact combo algorithm version.
    probability_stats, pair_hits, pair_n = combo_history_summary(
        db, now, btc_leg["direction"], eth_leg["direction"])
    pair_accuracy = pair_hits / pair_n if pair_n else 0.0
    log.info("Internal combo calibration: %s", probability_stats.replace("\n", " | "))
    if (COMBO_QUALITY_FILTERS_ENABLED and
            (pair_n < COMBO_MIN_SETTLED or pair_accuracy < COMBO_MIN_JOINT_ACCURACY)):
        record_combo_candidate(db, combo_key, btc_leg, eth_leg, now, btc_close, False)
        log.info(
            "Combo alert suppressed; shadow candidate retained: %s joint=%d/%d "
            "(%.1f%%), minimum=%d outcomes and %.1f%% accuracy",
            combo_key, pair_hits, pair_n, pair_accuracy*100,
            COMBO_MIN_SETTLED, COMBO_MIN_JOINT_ACCURACY*100)
        return
    btc_move_label = "Subida" if btc_leg["direction"] == "UP" else "Bajada"
    eth_move_label = "Subida" if eth_leg["direction"] == "UP" else "Bajada"
    eth_eta = max(0, int(eth_leg["eta_seconds"]))
    eth_eta_label = f"{eth_eta//60:02d}:{eth_eta%60:02d}"
    projection_horizon = min(max(0, int(remaining)), 300)
    projection_horizon_label = f"{projection_horizon//60:02d}:{projection_horizon%60:02d}"
    eth_sign = "+" if eth_leg["direction"] == "UP" else "-"
    if btc_leg["start"] is None:
        btc_block = (
            "₿ BTC — DIRECCIÓN KALSHI · KRAKEN PAUSADO PARA COMBOS\n"
            f"Dirección prevista: {direction_label(btc_leg['direction'])}\n"
            "Referencia numérica BTC/USD y proyección: pausadas en esta prueba.\n\n"
        )
    else:
        btc_sign = "+" if btc_leg["direction"] == "UP" else "-"
        btc_eta = max(0, int(btc_leg["eta_seconds"]))
        btc_eta_label = f"{btc_eta//60:02d}:{btc_eta%60:02d}"
        btc_block = (
            "₿ BTC — PRECIO DE REFERENCIA KRAKEN\n"
            f"Dirección prevista: {direction_label(btc_leg['direction'])}\n"
            f"Precio BTC de referencia: USD {btc_leg['start']:,.2f}\n"
            f"Movimiento estimado hasta el primer objetivo: {btc_sign}USD {btc_leg['move_amount']:,.2f}\n"
            f"Primer objetivo BTC: USD {btc_leg['target']:,.2f}\n"
            f"Precio BTC proyectado al horizonte: USD {btc_leg['projected']:,.2f}\n"
            f"Tiempo estimado hasta el primer objetivo: {btc_eta_label}\n\n"
        )
    message = (
        "🧩 COMBO BTC + ETH\n"
        f"⏳ Horizonte común de proyección: {projection_horizon_label}\n\n"
        f"{btc_block}"
        "◆ ETH — CONTRATO KALSHI YES\n"
        f"Dirección prevista: {direction_label(eth_leg['direction'])}\n"
        f"Cotización YES actual: USD {eth_leg['start']:.3f} por contrato\n"
        f"Variación estimada: {eth_sign}USD {eth_leg['move_amount']:.3f} por contrato\n"
        f"Cotización YES proyectada al horizonte: USD {eth_leg['projected']:.3f} por contrato\n"
        f"Tiempo estimado hasta el objetivo: {eth_eta_label}\n\n"
        "🏁 La liquidación oficial de BTC y ETH se informa por separado "
        "cuando Kalshi publique ambos resultados."
    )
    if not notify(message, db=db, broadcast=True):
        return
    record_combo_candidate(db, combo_key, btc_leg, eth_leg, now, btc_close, True)
    for leg in (btc_leg, eth_leg):
        db.execute("""INSERT OR IGNORE INTO kalshi_only_alerts
            (ticker,phase,sent_at,direction,bot_version,rules_snapshot)
            VALUES (?,?,?,?,?,?)""",
            (leg["ticker"], "COMBO_5M", now, leg["direction"], BOT_VERSION, RULES_SNAPSHOT))
    btc_features = btc_leg.get("model_features")
    if btc_features is not None:
        db.execute("""INSERT OR IGNORE INTO kalshi_prediction_feedback
            (id,ticker,phase,predicted_direction,issued_at,close_at,status,source,
             bot_version,rules_snapshot,model_features)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (f"kalshi-combo-btc:{combo_key}", btc_leg["ticker"], "COMBO_5M",
             btc_leg["direction"], now, btc_close, "pending", "bot_forecast",
             BOT_VERSION, RULES_SNAPSHOT,
             json.dumps(btc_features, separators=(",", ":"))))
    log.info("BTC+ETH combo sent: %s / %s", btc_leg["direction"], eth_leg["direction"])


def daily_bot_schedule(now=None):
    """Keep alerts active continuously; keep collecting and learning every scan."""
    instant = time.time() if now is None else now
    local_now = datetime.fromtimestamp(instant, ZoneInfo("America/New_York"))
    return "active", local_now


def scan(predictions_enabled=True):
    global last_results_check
    payload = get_json("/markets", {"series_ticker": SERIES, "status": "open", "limit": 100})
    eth_payload = get_json("/markets", {"series_ticker": ETH_SERIES, "status": "open", "limit": 100})
    now = time.time()
    if payload is None:
        with db_open() as db:
            record_kalshi_15m_chart(db, now)
            prepare_btc_opening_context(db, now)
            sync_dashboard(db, now)
        return
    # Keep spot histories fresh for learned feature vectors and combo price projections.
    kraken_trend(now)  # BTC/USD reference only; it does not choose UP or DOWN.
    with db_open() as db:
        record_kalshi_15m_chart(db, now)
        record_settled_eth_results(db, now)
        prepare_btc_opening_context(db, now)
        open_btc = []
        open_eth = []
        for market in payload.get("markets", []):
            ticker = market.get("ticker", "")
            if not isinstance(ticker, str) or not ticker.startswith(SERIES + "-"):
                continue
            opened, closes = ts(market.get("open_time")), ts(market.get("close_time"))
            if opened is None or closes is None or not (opened <= now < closes):
                continue
            if closes-opened > 960:
                continue
            q = quote(market)
            if q:
                sample(db, ticker, now, q[2])
                journal_opening_quote(db, market, now, opened, closes)
            open_btc.append(market)
            if predictions_enabled:
                signal(db, market, now, "EARLY_5M")
                if closes-now <= 300:
                    signal(db, market, now, "LAST_5M")
                # The next-contract forecast is sent as soon as its prior official result is available.
        if eth_payload is not None:
            for market in eth_payload.get("markets", []):
                ticker = market.get("ticker", "")
                if not isinstance(ticker, str) or not ticker.startswith(ETH_SERIES + "-"):
                    continue
                opened, closes = ts(market.get("open_time")), ts(market.get("close_time"))
                if opened is None or closes is None or not (opened <= now < closes):
                    continue
                if abs((closes-opened)-900) > 60:
                    continue
                q = quote(market)
                if q:
                    sample(db, ticker, now, q[2])
                    journal_opening_quote(db, market, now, opened, closes)
                open_eth.append(market)
        for btc_market in open_btc:
            btc_close = ts(btc_market.get("close_time"))
            for eth_market in open_eth:
                eth_close = ts(eth_market.get("close_time"))
                if (predictions_enabled and btc_close is not None and eth_close is not None
                        and abs(btc_close-eth_close) <= 2):
                    send_btc_eth_combo(db, btc_market, eth_market, now)
        if now - last_results_check >= 60:
            check_results(db, now)
            last_results_check = now
        maybe_train_adaptive_model(db, now)
        try:
            shadow_daily(db, now)
        except Exception:
            log.exception("Rafa shadow daily evaluation failed")
        db.execute("DELETE FROM kalshi_only_samples WHERE observed_at < ?", (now-48*3600,))
        db.commit()
        sync_dashboard(db, now)


def telegram_test_once():
    """Send one diagnostic message, without fabricating a market signal."""
    marker = os.path.join(os.path.dirname(DB_PATH) or ".", "telegram_test_20261122.ok")
    if os.path.exists(marker):
        return
    log.info("Telegram diagnostic: attempting test delivery")
    if notify("🤖 RAFA — PRUEBA DE TELEGRAM. Conexión funcionando. (No es señal de trading)"):
        log.info("Telegram diagnostic: API accepted test message")
        try:
            os.makedirs(os.path.dirname(marker) or ".", exist_ok=True)
            with open(marker, "w", encoding="utf-8") as out:
                out.write("sent\n")
        except OSError:
            log.exception("Telegram diagnostic marker could not be saved")
    else:
        log.error("Telegram diagnostic: test message failed or was not accepted")

def main():
    log.info("Kalshi BTC15M adaptive alerts and BTC+ETH combos; no automatic trading")
    btc_assistant.register_commands()
    previous_mode = None
    while True:
        mode, local_now = daily_bot_schedule()
        if mode != previous_mode:
            log.info("Daily schedule %s at %s (New York time)",
                     mode, local_now.strftime("%Y-%m-%d %H:%M %Z"))
            previous_mode = mode
        if mode == "paused":
            resume_at = local_now.replace(hour=6, minute=0, second=0, microsecond=0)
            time.sleep(min(60, max(1, (resume_at-local_now).total_seconds())))
            continue
        try:
            scan(predictions_enabled=(mode == "active"))
        except Exception:
            log.exception("Scan failed")
        try:
            with db_open() as db:
                btc_assistant.process_updates(db)
        except Exception:
            log.exception("Telegram assistant polling failed")
        time.sleep(SCAN_SECONDS)

if __name__ == "__main__":
    main()
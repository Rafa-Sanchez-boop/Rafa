"""Kalshi BTC 15-minute alerts with Kraken BTC/USD confirmation; no automatic trading.
Signals describe Kalshi YES/NO contract momentum, not BTC/USD price forecasts.
"""
import json
import logging
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import requests
import btc_assistant

BASE = "https://external-api.kalshi.com/trade-api/v2"
SERIES = os.getenv("KALSHI_SERIES", "KXBTC15M")
DB_PATH = os.getenv("DB_PATH", "/var/data/kalshi_bot.sqlite3")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SCAN_SECONDS = max(10, int(os.getenv("SCAN_SECONDS", "20")))
OBSERVE_SECONDS = max(60, int(os.getenv("OBSERVE_SECONDS", "60")))
EARLY_WINDOW_SECONDS = max(OBSERVE_SECONDS, int(os.getenv("EARLY_WINDOW_SECONDS", "300")))
LATE_WINDOW_SECONDS = max(OBSERVE_SECONDS, int(os.getenv("LATE_WINDOW_SECONDS", "300")))
MIN_VOLUME = Decimal(os.getenv("MIN_VOLUME_24H", "100"))
BOT_VERSION = os.getenv("BOT_VERSION", "rafa-kalshi-v4.5-dual-window")
PANEL_URL = os.getenv("RAFA_PANEL_URL", "").rstrip("/")
PANEL_SECRET = os.getenv("RAFA_PANEL_SECRET", "")
PANEL_SITES_AUTH = os.getenv("RAFA_PANEL_SITES_AUTH", "")
RULES_SNAPSHOT = json.dumps({
    "series": SERIES, "scan_seconds": SCAN_SECONDS,
    "observe_seconds": OBSERVE_SECONDS,
    "early_window_seconds": EARLY_WINDOW_SECONDS,
    "late_window_seconds": LATE_WINDOW_SECONDS,
    "minimum_volume_24h": str(MIN_VOLUME),
    "signal_rule": "Kalshi YES midpoint direction after the observation period; first and last contract windows; Kraken BTC/USD confirmation informational only",
    "automatic_orders": False,
}, separators=(",", ":"), sort_keys=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-only")
http = requests.Session()
http.headers["User-Agent"] = "KalshiOnlyBTC15M/4.5"
# Extra in-process protection against duplicate alerts during a worker lifetime.
alerted_phases = set()
KRAKEN_URL = "https://api.kraken.com/0/public/Ticker"
kraken_samples = []
kraken_last_quote = None

def kraken_trend(now):
    """Compare real Kraken BTC/USD last trades at least 60 seconds apart."""
    global kraken_last_quote
    try:
        response = http.get(KRAKEN_URL, params={"pair": "XBTUSD"}, timeout=10)
        response.raise_for_status()
        payload = response.json()
        if payload.get("error") or not payload.get("result"):
            log.warning("Kraken returned no valid ticker")
            return None
        item = next(iter(payload["result"].values()))
        price = dec(item["c"][0])
        if price is None or price <= 0:
            return None
        kraken_last_quote = (now, price)
        kraken_samples.append((now, price))
        kraken_samples[:] = [(t, p) for t, p in kraken_samples if now - t <= 600]
        prior = next((p for t, p in kraken_samples if t <= now - 60), None)
        if prior is None or price == prior:
            return None
        return "UP" if price > prior else "DOWN"
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError, StopIteration):
        log.exception("Kraken BTC/USD quote failed")
        return None

kraken_5m_snapshot = None

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
    projected = current + change_5m * Decimal(str(remaining)) / Decimal("300")
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
    db.execute("""INSERT OR IGNORE INTO kalshi_prediction_feedback
        (id, predicted_direction, actual_direction, status, source, note)
        VALUES ('user-report-2026-11-23', 'DOWN', 'UP', 'reported',
                'user_report',
                'User reported the bot forecast moved left/down; BTC actually rose right/up. Exact ticker, time, and prices were not provided.')""")
    db.execute("""INSERT OR IGNORE INTO kalshi_prediction_feedback
        (id, predicted_direction, actual_direction, start_price, target_price,
         actual_price, status, source, note)
        VALUES ('user-report-target-61388-61645', 'UP', 'REVERSAL_NEAR_LEVEL',
                '61388.40', '61645.95', '61388.40', 'reported',
                'user_report',
                'User reported the rise reversed near 61388.40; the bot target 61645.95 overshot the observed move. Exact ticker and timestamp were not provided.')""")
    db.execute("""INSERT OR IGNORE INTO kalshi_prediction_feedback
        (id, predicted_direction, actual_direction, status, source, note)
        VALUES ('user-report-final-down-screenshot-20261123', 'UP', 'DOWN',
                'reported', 'user_report',
                'User-provided BTC 15-minute screenshots show a falling chart and red move of USD 64.05 then USD 64.14 with 1%/99% displayed. User reports the bot forecast UP but the market finished DOWN. Objective text is truncated (about 61,300.5); exact ticker, timestamp, and final BTC price are unavailable, so this remains a manual report, not an OHLC-verified result.')""")
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

def notify(msg, db=None, broadcast=False, photo=None):
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

def make_model_features(phase, mid, kalshi_change, remaining):
    """Capture only values available when the forecast is issued."""
    snapshot = kraken_5m_snapshot
    if (snapshot is None or time.time() - snapshot[0] > 30
            or snapshot[1] <= 0):
        return None
    btc_return = float((snapshot[2] - snapshot[1]) / snapshot[1])
    return [
        1.0 if phase == "LAST_5M" else 0.0,
        float(mid),
        float(kalshi_change) * 10.0,
        btc_return * 1000.0,
        max(0.0, min(1.0, float(remaining) / 900.0)),
    ]


def _sigmoid(value):
    value = max(-30.0, min(30.0, value))
    return 1.0 / (1.0 + math.exp(-value))


def train_adaptive_model(db, now):
    """Fit a small local classifier from settled, out-of-sample bot forecasts."""
    rows = db.execute("""SELECT model_features, actual_direction
        FROM kalshi_prediction_feedback
        WHERE status='evaluated' AND source='bot_forecast'
          AND model_features IS NOT NULL
          AND actual_direction IN ('UP','DOWN')
        ORDER BY evaluated_at ASC""").fetchall()
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

    weights = [0.0] * 6
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
    """Use a validated model as a veto when it strongly disagrees with the signal."""
    if features is None:
        return True
    row = db.execute("SELECT model_json FROM adaptive_models WHERE name='btc_direction'").fetchone()
    if not row:
        return True
    try:
        model = json.loads(row[0])
        if (model.get("samples", 0) < 50
                or model.get("balanced_accuracy", 0) < 0.58):
            return True
        vector = [1.0] + [
            (value - mean) / scale
            for value, mean, scale in zip(features, model["means"], model["scales"])
        ]
        probability_up = _sigmoid(sum(w * x for w, x in zip(model["weights"], vector)))
        model_direction = "UP" if probability_up >= 0.5 else "DOWN"
        confidence = max(probability_up, 1.0 - probability_up)
        if model_direction != direction and confidence >= 0.58:
            log.info("Adaptive model vetoed %s signal: model favors %s (%.1f%%)",
                     direction, model_direction, confidence * 100)
            return False
    except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError):
        log.exception("Adaptive model could not read its saved parameters")
    return True


def maybe_train_adaptive_model(db, now):
    """Attempt one persisted model update per UTC day, after scored results arrive."""
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    row = db.execute("SELECT value FROM telegram_state WHERE key='adaptive_training_day'").fetchone()
    if row and row[0] == today:
        return
    train_adaptive_model(db, now)
    db.execute("""INSERT INTO telegram_state(key,value) VALUES ('adaptive_training_day',?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (today,))


def adaptive_direction_gate(db, phase, direction):
    """Pause a weak signal bucket using settled bot forecasts; never auto-reverse."""
    rows = db.execute("""SELECT actual_direction
        FROM kalshi_prediction_feedback
        WHERE status='evaluated' AND source='bot_forecast'
          AND phase=? AND predicted_direction=?
        ORDER BY evaluated_at DESC LIMIT 12""",
        (phase, direction)).fetchall()
    if len(rows) < 8:
        return True
    accuracy = sum(actual == direction for (actual,) in rows) / len(rows)
    if accuracy < 0.55:
        log.info("Adaptive filter paused %s %s: %d/%d recent forecasts matched",
                 phase, direction, sum(actual == direction for (actual,) in rows),
                 len(rows))
        return False
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
    """Experimental closing bias relative to the first target; not a reversal confirmation."""
    recent = sorted((t, p) for t, p in samples if now-120 <= t <= now)
    if len(recent) < 4 or remaining <= 0 or start <= 0:
        return None
    duration = recent[-1][0] - recent[0][0]
    if duration < 60 or now-recent[-1][0] > 30 or recent[-1][1] != start:
        return None
    if any(b[0]-a[0] > 60 for a, b in zip(recent, recent[1:])):
        return None
    prices = [p for _, p in recent]
    move = prices[-1] - prices[0]
    if not move:
        return None
    changes = [b-a for a, b in zip(prices, prices[1:]) if a != b]
    if len(changes) < 3 or 4*sum(c*move > 0 for c in changes) < 3*len(changes):
        return None
    noise = max(start*Decimal('0.0001'), (max(prices)-min(prices))/Decimal(2))
    if abs(move) < noise:
        return None
    short = start + move*Decimal(str(remaining))/Decimal(str(duration))
    blended = (short + projected)/Decimal(2)
    if short <= 0 or blended <= 0:
        return None
    if move > 0 and min(short, blended) > target+noise:
        return 'UP'
    if move < 0 and max(short, blended) < target-noise:
        return 'DOWN'
    return None


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


def forecast_display(ticker, phase_label, direction, confidence, start, target, projected):
    # The green/red directional signal is reserved for stronger displayed confidence.
    high = confidence is not None and confidence >= 90
    marker = ("🟢" if direction == "UP" else "🔴") if high else "🟡"
    projection = ("cierre alcista" if direction == "UP" else "cierre bajista") if high else "Dirección de cierre incierta"
    message = "📊 BTC KALSHI | 15 MIN\n\n"
    if high:
        message += (
            f"{direction_label(direction)}\n{confidence}%\nConfianza estimada del modelo\n"
            + "■"*(confidence//10) + "□"*(10-confidence//10) + "\n\n"
            + f"📈 Proyección: {projection} desde el precio de señal.\n"
            + f"BTC/USD: USD {start:,.2f} → USD {target:,.2f} (primer objetivo)\n"
            + f"📍 Cierre estimado: USD {projected:,.2f}\n"
        )
    else:
        message += "🟡 WAIT\nDirección de cierre incierta.\n"
    message += (
        "⏱️ Temporalidad: 15 minutos.\n"
        f"🎯 Nivel de confianza: {marker} {'ALTO' if high else 'INCIERTO'}\n"
        f"{phase_label} · {ticker}\n\n"
        "⚠️ Predicción estimada; no garantiza el resultado."
    )
    photo = None
    try:
        from io import BytesIO
        from PIL import Image, ImageDraw, ImageFont

        bg = "#08111f"
        panel = "#111d2e"
        muted = "#a9b8cc"
        fg = "#f5f8fc"
        accent = ("#21d07a" if direction == "UP" else "#ff5f68") if high else "#f6c945"
        canvas = Image.new("RGB", (900, 1000), bg)
        draw = ImageDraw.Draw(canvas)

        def font(size):
            return ImageFont.load_default(size=size)

        def centered(y, value, size, color=fg):
            draw.text((450, y), value, font=font(size), fill=color, anchor="mt")

        def fit(value, max_width, size):
            f = font(size)
            while size > 18 and draw.textbbox((0, 0), value, font=f)[2] > max_width:
                size -= 2
                f = font(size)
            return f

        draw.rounded_rectangle((20, 20, 880, 980), radius=34, fill=panel, outline="#26374d", width=3)
        centered(58, "BTC KALSHI  ·  15 MIN", 48)
        draw.rounded_rectangle((290, 112, 610, 164), radius=22, fill="#1b2b40")
        phase_font = fit(phase_label, 290, 25)
        draw.text((450, 138), phase_label, font=phase_font, fill=muted, anchor="mm")
        draw.line((70, 190, 830, 190), fill="#26374d", width=2)

        label = f"{direction} {'↑' if direction == 'UP' else '↓'}" if high else "WAIT"
        draw.rounded_rectangle((250, 220, 650, 355), radius=34, fill="#0b1625", outline=accent, width=3)
        centered(251, label, 88, accent)

        centered(385, f"{confidence}%" if confidence is not None else "—", 125, accent)
        centered(515, "CONFIANZA ESTIMADA", 30, muted)
        draw.rounded_rectangle((100, 562, 800, 596), radius=17, fill="#26374d")
        if confidence is not None:
            fill_width = int(700 * max(0, min(100, confidence)) / 100)
            if fill_width > 0:
                draw.rounded_rectangle((100, 562, 100 + fill_width, 596), radius=17, fill=accent)

        centered(635, "PROYECCIÓN", 27, muted)
        centered(674, projection.upper(), 37, accent if high else muted)

        draw.rounded_rectangle((68, 728, 832, 850), radius=24, fill="#0b1625")
        centered(744, "BTC/USD  ·  PRIMER OBJETIVO", 23, muted)
        price_line = f"USD {start:,.2f}  →  USD {target:,.2f}"
        draw.text((450, 800), price_line, font=fit(price_line, 700, 35), fill=fg, anchor="mm")

        centered(872, f"CIERRE ESTIMADO  ·  USD {projected:,.2f}", 31, fg)
        draw.ellipse((82, 925, 108, 951), fill=accent)
        status = "NIVEL DE CONFIANZA: ALTO" if high else "NIVEL DE CONFIANZA: INCIERTO"
        draw.text((450, 938), status, font=fit(status, 650, 28), fill=accent, anchor="mm")
        centered(963, "Proyección experimental; no garantiza el resultado.", 22, muted)

        buffer = BytesIO()
        canvas.save(buffer, format="PNG")
        photo = buffer.getvalue()
    except Exception:
        log.exception("Rafa signal card unavailable; using text")
    return message, photo

def signal(db, m, now, phase, kraken_direction=None):
    """Send a Kraken BTC/USD forecast; keep Kalshi settlement separate."""
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
    if volume is None or volume < MIN_VOLUME:
        return
    close_at = ts(m.get("close_time"))
    if close_at is None or close_at <= now:
        return
    if not kraken_trend_5m(now):
        return
    projection = kraken_close_projection(now, close_at-now)
    if projection is None:
        return
    start, projected = projection
    direction = "UP" if projected > start else "DOWN"
    first_projection = kraken_nearest_level_projection(now, direction, projection)
    if first_projection is None or first_projection[1] == start:
        return
    first_target = first_projection[1]
    bias = target_close_bias(now, close_at-now, start, first_target, projected, kraken_samples)
    bias_text = {"UP": "🟢 UP · sesgo de cierre por encima del objetivo",
                 "DOWN": "🔴 DOWN · sesgo de cierre por debajo del objetivo"}.get(
                     bias, "🟡 No confirmado · evidencia insuficiente")
    db.execute("""CREATE TABLE IF NOT EXISTS rafa_close_estimates
        (id TEXT PRIMARY KEY, estimated TEXT, absolute_error TEXT)""")
    change = Decimal("0")
    confirmation = "Kraken BTC/USD"
    phase_label = "PRIMEROS 5 MIN" if phase == "EARLY_5M" else "ÚLTIMOS 5 MIN"
    confidence = closing_confidence(db, ticker, phase, direction, now, close_at-now, start, projected)
    message, photo = forecast_display(ticker, phase_label, direction, confidence,
                                      start, first_target, projected)
    if notify(message, db=db, broadcast=True, photo=photo):
        alerted_phases.add(alert_key)
        db.execute("""INSERT OR IGNORE INTO kalshi_only_alerts
            (ticker,phase,sent_at,direction,bot_version,rules_snapshot)
            VALUES (?,?,?,?,?,?)""",
            (ticker, phase, now, direction, BOT_VERSION, RULES_SNAPSHOT))
        db.execute("""INSERT OR IGNORE INTO kalshi_prediction_feedback
            (id,ticker,phase,predicted_direction,issued_at,close_at,start_price,target_price,status,source,bot_version,rules_snapshot)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"kraken-primary:{ticker}:{phase}",ticker,phase,direction,now,close_at,str(start),str(first_target),"pending","bot_forecast",BOT_VERSION,"Kraken BTC/USD separate first target and close estimate"))
        db.execute("INSERT OR IGNORE INTO rafa_close_estimates (id,estimated) VALUES (?,?)",
                   (f"kraken-primary:{ticker}:{phase}",str(projected)))
        try:
            shadow_record(db, ticker, phase, now, direction, q, change, volume, kraken_direction)
        except Exception:
            log.exception("Rafa shadow recording failed")
        log.info("Kalshi %s forecast: %s %s; Kraken=%s (%s)", phase, ticker, direction, kraken_direction, confirmation)

def evaluate_btc_forecasts(db, now):
    """Score forecast direction and whether its first target was touched before expiry."""
    rows = db.execute("""SELECT id, ticker, phase, predicted_direction, close_at,
                                start_price, target_price, issued_at
                         FROM kalshi_prediction_feedback
                         WHERE status='pending' AND close_at <= ?
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
                SET actual_direction=?, actual_price=?, evaluated_at=?,
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
            notify("🧠 BTC — REVISIÓN DE PREDICCIÓN\n"
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

def check_results(db, now):
    # Separately score BTC/USD price forecasts against Kraken. Kalshi settlement
    # below remains the official outcome for the prediction-market contract.
    evaluate_btc_forecasts(db, now)
    # Use Kalshi's official result field, never Kraken, for completed markets.
    rows = db.execute("""SELECT DISTINCT ticker FROM kalshi_only_alerts
        WHERE ticker NOT IN (SELECT ticker FROM kalshi_only_results)""").fetchall()
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
            correct = (direction == "UP" and result == "yes") or (
                direction == "DOWN" and result == "no")
            notify(f"🧠 BTC — REVISIÓN DE PREDICCIÓN\n🧾 {ticker}\n"
                   f"🔮 Pronóstico Kalshi: {direction_label(direction)}\n"
                   f"🏁 Resultado oficial: {result.upper()}\n"
                   f"{'✅ Coincidió' if correct else '❌ No coincidió'} con el resultado oficial.",
                   db=db, broadcast=True)

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

def scan():
    payload = get_json("/markets", {"series_ticker": SERIES, "status": "open", "limit": 100})
    now = time.time()
    if payload is None:
        with db_open() as db:
            record_kalshi_15m_chart(db, now)
            sync_dashboard(db, now)
        return
    kraken_direction = kraken_trend(now)
    with db_open() as db:
        record_kalshi_15m_chart(db, now)
        for m in payload.get("markets", []):
            ticker = m.get("ticker", "")
            if not isinstance(ticker, str) or not ticker.startswith(SERIES + "-"):
                continue
            opened, closes = ts(m.get("open_time")), ts(m.get("close_time"))
            if opened is None or closes is None or not (opened <= now < closes):
                continue
            if closes - opened > 960:
                continue
            q = quote(m)
            if q:
                sample(db, ticker, now, q[2])
                journal_opening_quote(db, m, now, opened, closes)
            elapsed = now - opened
            remaining = closes - now
            phase = None
            if OBSERVE_SECONDS <= elapsed <= EARLY_WINDOW_SECONDS:
                phase = "EARLY_5M"
            elif 0 < remaining <= LATE_WINDOW_SECONDS:
                phase = "LAST_5M"
            if phase:
                signal(db, m, now, phase, kraken_direction)
        # Check completed markets infrequently to reduce requests.
        if int(now // 300) != int((now - SCAN_SECONDS) // 300):
            check_results(db, now)
        maybe_train_adaptive_model(db, now)
        try:
            shadow_daily(db, now)
        except Exception:
            log.exception('Rafa shadow daily evaluation failed')
        db.execute("DELETE FROM kalshi_only_samples WHERE observed_at < ?", (now - 48 * 3600,))
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
    log.info("Kalshi BTC15M quote-momentum alerts and Telegram BTC assistant; no automatic trading")
    btc_assistant.register_commands()
    while True:
        try:
            scan()
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

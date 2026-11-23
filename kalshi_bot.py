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
DELAY = 0
MIN_REMAINING = max(300, int(os.getenv("MIN_REMAINING_SECONDS", "300")))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-only")
http = requests.Session()
http.headers["User-Agent"] = "KalshiOnlyBTC15M/4.5"
# Extra in-process protection against duplicate alerts during a worker lifetime.
alerted_tickers = set()
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
    db.execute("""CREATE TABLE IF NOT EXISTS kalshi_only_samples
               (ticker TEXT, observed_at REAL, yes_mid TEXT,
                PRIMARY KEY(ticker, observed_at))""")
    # Persist completed Kraken BTC/USD 15-minute candles for the visual streak chart.
    db.execute("""CREATE TABLE IF NOT EXISTS btc_15m_candles
               (open_time INTEGER PRIMARY KEY, close_time INTEGER NOT NULL,
                open_price TEXT NOT NULL, close_price TEXT NOT NULL,
                direction TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS btc_15m_chart_sent
               (close_time INTEGER PRIMARY KEY)""")
    db.execute("""CREATE TABLE IF NOT EXISTS kalshi_only_alerts
               (ticker TEXT, phase TEXT, sent_at REAL, direction TEXT,
                PRIMARY KEY(ticker,phase))""")
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
            ("model_features", "TEXT")):
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

def notify(msg):
    if not TOKEN or not CHAT_ID:
        log.warning("Telegram not configured: %s", msg)
        return False
    try:
        r = http.post("https://api.telegram.org/bot" + TOKEN + "/sendMessage",
                      json={"chat_id": CHAT_ID, "text": msg}, timeout=15)
        r.raise_for_status()
        return bool(r.json().get("ok"))
    except (requests.RequestException, ValueError):
        log.exception("Telegram send failed")
        return False

def quote(m):
    bid, ask = dec(m.get("yes_bid_dollars")), dec(m.get("yes_ask_dollars"))
    if bid is None or ask is None or not (Decimal(0) < bid <= ask < Decimal(1)):
        return None
    return bid, ask, (bid + ask) / 2

def sample(db, ticker, now, mid):
    db.execute("INSERT OR IGNORE INTO kalshi_only_samples VALUES (?,?,?)",
               (ticker, now, str(mid)))

def record_btc_15m_chart(db, now):
    """Store completed Kraken BTC/USD 15m candles and send a compact streak chart."""
    try:
        response = http.get(KRAKEN_URL.replace("/Ticker", "/OHLC"),
                            params={"pair": "XBTUSD", "interval": 15}, timeout=15)
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result") or {}
        key = next((name for name in result if name != "last"), None)
        candles = result.get(key, []) if key else []
        completed = []
        for candle in candles:
            if len(candle) < 5:
                continue
            open_time = int(float(candle[0]))
            close_time = open_time + 15 * 60
            if close_time > now:
                continue
            open_price, close_price = dec(candle[1]), dec(candle[4])
            if open_price is None or close_price is None or open_price <= 0:
                continue
            direction = ("UP" if close_price > open_price else
                         "DOWN" if close_price < open_price else "FLAT")
            completed.append((open_time, close_time, str(open_price),
                              str(close_price), direction))
        for row in completed:
            db.execute("""INSERT OR IGNORE INTO btc_15m_candles
                (open_time, close_time, open_price, close_price, direction)
                VALUES (?,?,?,?,?)""", row)
        db.execute("DELETE FROM btc_15m_candles WHERE close_time < ?",
                   (int(now) - 30 * 24 * 60 * 60,))
        latest = db.execute("""SELECT open_time, close_time, open_price,
                                      close_price, direction
                               FROM btc_15m_candles
                               ORDER BY close_time DESC LIMIT 1""").fetchone()
        if latest is None:
            return
        open_time, close_time, open_text, close_text, direction = latest
        if db.execute("SELECT 1 FROM btc_15m_chart_sent WHERE close_time=?",
                      (close_time,)).fetchone():
            return
        rows = db.execute("""SELECT open_time, close_time, open_price,
                                    close_price, direction
                             FROM btc_15m_candles
                             ORDER BY close_time DESC LIMIT 16""").fetchall()
        rows.reverse()
        last_four = rows[-4:]
        up_16 = sum(row[4] == "UP" for row in rows)
        down_16 = sum(row[4] == "DOWN" for row in rows)
        up_4 = sum(row[4] == "UP" for row in last_four)
        down_4 = sum(row[4] == "DOWN" for row in last_four)
        markers = {"UP": "🟩", "DOWN": "🟥", "FLAT": "⬜"}
        chart = "".join(markers[row[4]] for row in rows)
        streak = 0
        if direction != "FLAT":
            for row in reversed(rows):
                if row[4] != direction:
                    break
                streak += 1
        arrow = "🔺 UP" if direction == "UP" else "🔻 DOWN" if direction == "DOWN" else "➖ FLAT"
        close_label = datetime.fromtimestamp(close_time, timezone.utc).strftime("%H:%M UTC")
        message = (
            "📊 BTC/USD · cierres de 15 min (Kraken)\n"
            f"🟥 DOWN | 🟩 UP · antiguo → reciente\n{chart}\n"
            f"Últimas 4: DOWN {down_4} · UP {up_4}\n"
            f"Últimas 16: DOWN {down_16} · UP {up_16}\n"
            f"Racha actual: {arrow} × {streak}\n"
            f"Cierre {close_label}: ${dec(open_text):,.2f} → ${dec(close_text):,.2f}"
        )
        if notify(message):
            db.execute("INSERT OR IGNORE INTO btc_15m_chart_sent VALUES (?)",
                       (close_time,))
    except (requests.RequestException, ValueError, KeyError, IndexError,
            TypeError, StopIteration):
        log.exception("Could not update BTC 15-minute candle chart")

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

def signal(db, m, now, close_at, open_at, phase, btc_trend):
    ticker = m["ticker"]
    alert_key = (ticker, phase)
    if alert_key in alerted_tickers:
        return
    if db.execute("SELECT 1 FROM kalshi_only_alerts WHERE ticker=? AND phase=?",
                  (ticker, phase)).fetchone():
        alerted_tickers.add(alert_key)
        return
    q = quote(m)
    if q is None:
        return
    bid, ask, mid = q
    elapsed = now - open_at
    remaining = close_at - now
    if phase == "EARLY_5M":
        if elapsed < 0 or elapsed > 300:
            return
        # Require an observed Kalshi quote at least 60 seconds earlier.
        rows = db.execute("""SELECT yes_mid FROM kalshi_only_samples
            WHERE ticker=? AND observed_at<=? AND observed_at>=?
            ORDER BY observed_at DESC LIMIT 1""",
            (ticker, now - 60, open_at - 120)).fetchone()
        if not rows:
            log.info("Waiting for an earlier Kalshi quote for %s", ticker)
            return
        prior = dec(rows[0])
    elif phase == "LAST_5M":
        if remaining < 0 or remaining > 300:
            return
        # Compare Kalshi's YES midpoint with the closest real quote to the
        # start of the same five-minute interval.
        target = now - 300
        rows = db.execute("""SELECT yes_mid, observed_at FROM kalshi_only_samples
            WHERE ticker=? AND observed_at BETWEEN ? AND ?
            ORDER BY ABS(observed_at - ?) LIMIT 1""",
            (ticker, target - 45, target + 45, target)).fetchone()
        if not rows:
            log.info("Waiting for five-minute Kalshi history for %s", ticker)
            return
        prior = dec(rows[0])
        if prior is None or abs(rows[1] - target) > 45:
            return
    else:
        return
    if prior is None:
        return
    change = mid - prior
    if change == 0:
        return
    up = change > 0
    direction = "UP" if up else "DOWN"
    if btc_trend != direction:
        log.info("Skipping %s %s: Kalshi %s, Kraken 5m %s",
                 phase, ticker, direction, btc_trend)
        return
    if not adaptive_direction_gate(db, phase, direction):
        return
    model_features = make_model_features(phase, mid, change, remaining)
    if not adaptive_model_allows(db, model_features, direction):
        return
    btc_projection = kraken_close_projection(now, remaining)
    if btc_projection is None:
        log.info("Skipping %s %s: no fresh five-minute BTC/USD data for estimate",
                 phase, ticker)
        return
    btc_projection = kraken_nearest_level_projection(now, direction, btc_projection)
    if btc_projection is None:
        log.info("Skipping %s %s: no fresh BTC quote for level target", phase, ticker)
        return
    btc_now, btc_close = btc_projection
    if (direction == "UP" and btc_close <= btc_now) or (
            direction == "DOWN" and btc_close >= btc_now):
        log.info("Skipping %s %s: no remaining move to the next level", phase, ticker)
        return
    move = btc_close - btc_now
    move_word = "subida" if move > 0 else "bajada"
    phase_name = ("primeros cinco minutos" if phase == "EARLY_5M"
                  else "últimos cinco minutos")
    message = (
        ("🔺 UP" if up else "🔻 DOWN") + "\n"
        + f"📈 BTC: posible {move_word} desde ${btc_now:,.2f} "
        + f"hasta ${btc_close:,.2f} ({move:+,.2f} USD).\n"
        + "🎯 Primer tope estimado; después el mercado puede cambiar."
    )
    if notify(message):
        alerted_tickers.add(alert_key)
        db.execute("INSERT OR IGNORE INTO kalshi_only_alerts VALUES (?,?,?,?)",
                   (ticker, phase, now, direction))
        db.execute("""INSERT OR IGNORE INTO kalshi_prediction_feedback
            (id, ticker, phase, predicted_direction, issued_at, close_at,
             start_price, target_price, model_features, status, source, note)
            VALUES (?,?,?,?,?,?,?,?,?, 'pending', 'bot_forecast', ?)""",
            (ticker + ":" + phase, ticker, phase, direction, now, close_at,
             str(btc_now), str(btc_close),
             json.dumps(model_features, separators=(",", ":")) if model_features is not None else None,
             "Awaiting Kraken BTC/USD close-price evaluation."))
        log.info("Kalshi %s alert: %s %s", phase, ticker, direction)

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
                      and float(c[0]) <= close_at + 1
                      and float(c[0]) + 60 >= issued_at]
            if not window:
                continue
            close_candle = min(window, key=lambda c: abs(float(c[0]) - close_at))
            if abs(float(close_candle[0]) - close_at) > 60:
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
            target_line = ("Primer objetivo: tocado" if reached else
                           "Primer objetivo: no observado" if reached is False else
                           "Primer objetivo: sin dato")
            reversal_line = ("🔄 El precio tocó el objetivo y luego revirtió antes del cierre.\n"
                             if reached and actual != predicted else "")
            notify("🧠 BTC — REVISIÓN DE PREDICCIÓN\n"
                   f"🧾 {ticker} ({phase})\n"
                   f"🔮 Pronóstico: {predicted} desde USD {start_price:,.2f}\n"
                   f"📈 Máximo/mínimo después de la señal: USD {peak:,.2f} / USD {trough:,.2f}\n"
                   f"🎯 {target_line} ({'USD ' + format(target_price, ',.2f') if target_price is not None else 'sin dato'})\n"
                   f"📍 Cierre aproximado: {actual} en USD {actual_price:,.2f}\n"
                   + reversal_line
                   + ("✅ Dirección al cierre coincidió." if correct else
                      "❌ Dirección al cierre falló; datos guardados."))
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
            notify(f"📊 KALSHI — RESULTADO OFICIAL\n🧾 {ticker}\n"
                   f"📈 Dirección de momentum observada: {direction} ({phase})\n"
                   f"🏁 Resultado final Kalshi: {result.upper()}\n"
                   f"{'Coincidió' if correct else 'No coincidió'} con el resultado final.\n"
                   "⚠️ La señal mide momentum de cotización, no predice "
                   "el resultado final ni confirma beneficio de compraventa.")

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

def scan():
    payload = get_json("/markets", {"series_ticker": SERIES, "status": "open", "limit": 100})
    now = time.time()
    if payload is None:
        with db_open() as db:
            record_btc_15m_chart(db, now)
        return
    btc_trend = kraken_trend(now)
    with db_open() as db:
        record_btc_15m_chart(db, now)
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
            # One prediction opportunity during the opening five minutes and
            # one during the final five minutes before settlement.
            trend_5m = kraken_trend_5m(now)
            if 0 <= elapsed <= 300:
                signal(db, m, now, closes, opened, "EARLY_5M", trend_5m)
            if 0 <= remaining <= 300:
                signal(db, m, now, closes, opened, "LAST_5M", trend_5m)
        # Check completed markets infrequently to reduce requests.
        if int(now // 300) != int((now - SCAN_SECONDS) // 300):
            check_results(db, now)
        maybe_train_adaptive_model(db, now)
        db.execute("DELETE FROM kalshi_only_samples WHERE observed_at < ?", (now - 48 * 3600,))
        db.commit()

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
    log.info("Kalshi BTC15M with Kraken confirmation and Telegram BTC assistant")
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

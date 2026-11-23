"""Kalshi BTC 15-minute alerts with Kraken BTC/USD confirmation; no automatic trading.
Signals describe Kalshi YES/NO contract momentum, not BTC/USD price forecasts.
"""
import logging
import os
import sqlite3
import time
from datetime import datetime
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

def kraken_trend_5m(now):
    """Return BTC direction over a complete, recent five-minute window."""
    if not kraken_samples:
        return None
    target = now - 300
    prior = min(kraken_samples, key=lambda sample: abs(sample[0] - target))
    current = kraken_samples[-1]
    if abs(prior[0] - target) > 45 or current[0] < now - 30:
        return None
    if prior[1] == current[1]:
        return None
    return "UP" if current[1] > prior[1] else "DOWN"

def kraken_close_projection(now, remaining):
    """Estimate BTC/USD at contract close by extending the latest five-minute move."""
    if not kraken_samples or kraken_last_quote is None:
        return None
    quote_time, current = kraken_last_quote
    if now - quote_time > 30 or remaining < 0:
        return None
    target_time = now - 300
    prior_time, prior = min(kraken_samples, key=lambda sample: abs(sample[0] - target_time))
    if abs(prior_time - target_time) > 45 or current == prior:
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
            if recent_ceiling is not None and recent_ceiling >= current * Decimal("0.9995"):
                log.info("Skipping UP projection: BTC is already at recent resistance %s", recent_ceiling)
                return current, current
        else:
            levels = [level for level in lows if level < current]
            level = max(levels) if levels else None
            recent_floor = min(lows) if lows else None
            if recent_floor is not None and recent_floor <= current * Decimal("1.0005"):
                log.info("Skipping DOWN projection: BTC is already at recent support %s", recent_floor)
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
        + "🔄 El mercado puede cambiar de rumbo; objetivo estimado."
    )
    if notify(message):
        alerted_tickers.add(alert_key)
        db.execute("INSERT OR IGNORE INTO kalshi_only_alerts VALUES (?,?,?,?)",
                   (ticker, phase, now, direction))
        db.execute("""INSERT OR IGNORE INTO kalshi_prediction_feedback
            (id, ticker, phase, predicted_direction, issued_at, close_at,
             start_price, target_price, status, source, note)
            VALUES (?,?,?,?,?,?,?,?, 'pending', 'bot_forecast', ?)""",
            (ticker + ":" + phase, ticker, phase, direction, now, close_at,
             str(btc_now), str(btc_close),
             "Awaiting Kraken BTC/USD close-price evaluation."))
        log.info("Kalshi %s alert: %s %s", phase, ticker, direction)

def evaluate_btc_forecasts(db, now):
    """Score pending BTC/USD forecasts using Kraken's one-minute close near expiry."""
    rows = db.execute("""SELECT id, ticker, phase, predicted_direction, close_at,
                                start_price
                         FROM kalshi_prediction_feedback
                         WHERE status='pending' AND close_at <= ?
                         ORDER BY close_at LIMIT 100""", (now,)).fetchall()
    for row in rows:
        forecast_id, ticker, phase, predicted, close_at, start_text = row
        try:
            response = http.get(
                "https://api.kraken.com/0/public/OHLC",
                params={"pair": "XBTUSD", "interval": 1,
                        "since": max(0, int(close_at) - 60)},
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
            candle = min(candles, key=lambda c: abs(float(c[0]) - close_at),
                         default=None)
            if candle is None or abs(float(candle[0]) - close_at) > 60:
                continue
            actual_price = dec(candle[4])
            start_price = dec(start_text)
            if actual_price is None or start_price is None:
                continue
            actual = ("UP" if actual_price > start_price else
                      "DOWN" if actual_price < start_price else "FLAT")
            correct = predicted == actual
            db.execute("""UPDATE kalshi_prediction_feedback
                SET actual_direction=?, actual_price=?, evaluated_at=?,
                    status='evaluated', note=? WHERE id=? AND status='pending'""",
                (actual, str(actual_price), now,
                 "Direction matched" if correct else "Direction missed",
                 forecast_id))
            notify("🧠 BTC — REVISIÓN DE PREDICCIÓN\\n"
                   f"🧾 {ticker} ({phase})\\n"
                   f"🔮 Pronóstico: {predicted} desde ${start_price:,.2f}\\n"
                   f"📍 Resultado cerca del cierre: {actual} a ${actual_price:,.2f}\\n"
                   f"{'✅ Coincidió' if correct else '❌ Falló; error guardado'}\\n"
                   "El resultado quedó guardado para medir el desempeño futuro.")
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
    if payload is None:
        return
    now = time.time()
    btc_trend = kraken_trend(now)
    with db_open() as db:
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
                out.write("sent\\n")
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

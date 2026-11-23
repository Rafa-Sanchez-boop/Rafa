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
    db.execute("""CREATE TABLE IF NOT EXISTS kalshi_only_samples
               (ticker TEXT, observed_at REAL, yes_mid TEXT,
                PRIMARY KEY(ticker, observed_at))""")
    db.execute("""CREATE TABLE IF NOT EXISTS kalshi_only_alerts
               (ticker TEXT, phase TEXT, sent_at REAL, direction TEXT,
                PRIMARY KEY(ticker,phase))""")
    db.execute("""CREATE TABLE IF NOT EXISTS kalshi_only_results
               (ticker TEXT PRIMARY KEY, result TEXT, observed_at REAL)""")
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
    btc_now, btc_close = btc_projection
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
        log.info("Kalshi %s alert: %s %s", phase, ticker, direction)

def check_results(db, now):
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
    log.info("Kalshi BTC15M with Kraken confirmation and short UP/DOWN messages")
    while True:
        try:
            scan()
        except Exception:
            log.exception("Scan failed")
        time.sleep(SCAN_SECONDS)

if __name__ == "__main__":
    main()

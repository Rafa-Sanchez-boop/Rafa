"""BTC 15-minute Kalshi-only contract signals; no trading."""
import json
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
DELAY = 120
MAX_SPREAD = Decimal(os.getenv("MAX_SPREAD", "0.04"))
MIN_MOVE = Decimal(os.getenv("MIN_MOVE", "0.025"))
MIN_VOLUME = Decimal(os.getenv("MIN_VOLUME_24H", "100"))
MIN_REMAINING = 0  # Allow alerts until contract closes, including final 2-3 minutes.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-only")
http = requests.Session()
http.headers["User-Agent"] = "KalshiBTC15M/5.1"
# Extra in-process protection against opposite alerts during a worker lifetime.
alerted_tickers = set()

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
    db.execute("""CREATE TABLE IF NOT EXISTS signal_features
               (ticker TEXT PRIMARY KEY, direction TEXT, move REAL,
                remaining_seconds INTEGER, recorded_at REAL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS learning_audit
               (ticker TEXT PRIMARY KEY, correct INTEGER, assessed_at REAL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS daily_learning
               (day TEXT PRIMARY KEY, total INTEGER, correct INTEGER,
                up_total INTEGER, up_correct INTEGER,
                down_total INTEGER, down_correct INTEGER,
                generated_at REAL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS market_observations
               (ticker TEXT, observed_at REAL, yes_bid TEXT, yes_ask TEXT,
                yes_mid TEXT, volume TEXT, PRIMARY KEY(ticker, observed_at))""")
    db.execute("""CREATE TABLE IF NOT EXISTS trained_models
               (day TEXT PRIMARY KEY, trained_at REAL, samples INTEGER,
                validation_count INTEGER, validation_brier REAL,
                baseline_brier REAL, active INTEGER, model_json TEXT)""")
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
        accepted = bool(r.json().get("ok"))
        if accepted:
            log.info("Telegram API accepted sendMessage")
        else:
            log.error("Telegram API rejected sendMessage")
        return accepted
    except (requests.RequestException, ValueError):
        log.exception("Telegram send failed")
        return False

def quote(m):
    bid, ask = dec(m.get("yes_bid_dollars")), dec(m.get("yes_ask_dollars"))
    if bid is None or ask is None or not (Decimal(0) < bid <= ask < Decimal(1)):
        return None
    if ask - bid > MAX_SPREAD:
        return None
    return bid, ask, (bid + ask) / 2

def sample(db, ticker, now, mid):
    db.execute("INSERT OR IGNORE INTO kalshi_only_samples VALUES (?,?,?)",
               (ticker, now, str(mid)))

def performance(db, direction=None):
    """Evaluate only settled, previously emitted alerts; no look-ahead."""
    where = "AND a.direction=?" if direction else ""
    params = (direction,) if direction else ()
    rows = db.execute("""SELECT a.direction, r.result
        FROM kalshi_only_alerts a JOIN kalshi_only_results r ON r.ticker=a.ticker
        WHERE a.phase='OPEN' AND r.result IN ('yes','no') """ + where, params).fetchall()
    n = len(rows)
    hits = sum((d == "UP" and result == "yes") or
               (d == "DOWN" and result == "no") for d, result in rows)
    return n, hits


def learning_summary(db, direction):
    """Conservative empirical reliability with a 10-signal minimum."""
    n, hits = performance(db, direction)
    if n < 10:
        return f"🧠 Aprendizaje {direction}: {n}/10 resultados mínimos; sin ajuste aún."
    rate = hits / n
    # A smoothed historical agreement rate is NOT a validated probability.
    smoothed = (hits + 2) / (n + 4)
    note = ("historial favorable" if smoothed >= 0.60 else
            "historial débil; revisar señales" if smoothed < 0.45 else
            "historial sin ventaja clara")
    return (f"🧠 Aprendizaje {direction}: {hits}/{n} coincidencias "
            f"({rate:.1%}); evaluación suavizada {smoothed:.1%}, {note}. "
            "No es probabilidad de ganar.")


def accuracy_report(db):
    n, hits = performance(db)
    up_n, up_hits = performance(db, "UP")
    down_n, down_hits = performance(db, "DOWN")
    if not n:
        return "📚 Precisión histórica: sin contratos alertados y liquidados todavía."
    return (f"📚 Precisión histórica (coincidencia con liquidación): "
            f"{hits}/{n} = {hits/n:.1%}\\n"
            f"🔺 UP: {up_hits}/{up_n} | 🔻 DOWN: {down_hits}/{down_n}\\n"
            "⚠️ No equivale a rentabilidad ni precisión predictiva validada.")

def feature_bucket(direction, move, remaining):
    return f"{direction}:{'large' if abs(float(move)) >= 0.10 else 'small'}:{'late' if int(remaining) < 300 else 'early'}"


def retrain_daily(db, now):
    """Retrain from settled alerts; activate only if temporal holdout improves Brier score."""
    day = datetime.utcfromtimestamp(now).strftime("%Y-%m-%d")
    if db.execute("SELECT 1 FROM trained_models WHERE day=?", (day,)).fetchone():
        return
    rows = db.execute("""SELECT f.direction, f.move, f.remaining_seconds, a.sent_at, r.result
        FROM signal_features f JOIN kalshi_only_alerts a
        ON a.ticker=f.ticker AND a.phase='OPEN'
        JOIN kalshi_only_results r ON r.ticker=f.ticker
        WHERE r.result IN ('yes','no') ORDER BY a.sent_at, f.ticker""").fetchall()
    if len(rows) < 60:
        return
    cut = int(len(rows) * 0.8)
    training, validation = rows[:cut], rows[cut:]
    def outcome(row):
        return int((row[0] == "UP" and row[4] == "yes") or
                   (row[0] == "DOWN" and row[4] == "no"))
    counts = {}
    for row in training:
        key = feature_bucket(row[0], row[1], row[2])
        n, hits = counts.get(key, (0, 0))
        counts[key] = (n + 1, hits + outcome(row))
    base = (sum(outcome(x) for x in training) + 2) / (len(training) + 4)
    def predict(row):
        n, hits = counts.get(feature_bucket(row[0], row[1], row[2]), (0, 0))
        return (hits + 8 * base) / (n + 8)
    score = sum((predict(x) - outcome(x)) ** 2 for x in validation) / len(validation)
    baseline = sum((base - outcome(x)) ** 2 for x in validation) / len(validation)
    active = int(score + 0.002 < baseline)
    db.execute("INSERT INTO trained_models VALUES (?,?,?,?,?,?,?,?)",
               (day, now, len(training), len(validation), score, baseline,
                active, json.dumps({"counts": counts, "base": base})))
    log.info("Daily model %s: %d train %d validation, brier %.4f vs %.4f, active=%s",
             day, len(training), len(validation), score, baseline, bool(active))


def model_assessment(db, direction, change, remaining):
    row = db.execute("""SELECT day, model_json FROM trained_models
        WHERE active=1 ORDER BY day DESC LIMIT 1""").fetchone()
    if not row:
        return "🤖 Modelo diario: esperando al menos 60 resultados oficiales para validar."
    model = json.loads(row[1])
    n, hits = model["counts"].get(feature_bucket(direction, change, remaining), [0, 0])
    estimate = (hits + 8 * model["base"]) / (n + 8)
    return (f"🤖 Modelo {row[0]}: coincidencia histórica estimada {estimate:.1%} "
            "para señales similares; no es garantía ni rentabilidad.")


def daily_recalibration(db, now):
    """Daily historical checkpoint; no unvalidated live strategy changes."""
    day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    if db.execute("SELECT 1 FROM daily_learning WHERE day=?", (day,)).fetchone():
        return
    n, hits = performance(db)
    up_n, up_hits = performance(db, "UP")
    down_n, down_hits = performance(db, "DOWN")
    db.execute("INSERT OR IGNORE INTO daily_learning VALUES (?,?,?,?,?,?,?,?)",
               (day, n, hits, up_n, up_hits, down_n, down_hits, now))
    log.info("Daily learning checkpoint %s: total=%d correct=%d UP=%d/%d DOWN=%d/%d",
             day, n, hits, up_hits, up_n, down_hits, down_n)


def signal(db, m, now, close_at, open_at, phase):
    ticker = m["ticker"]
    if ticker in alerted_tickers:
        return
    if db.execute("SELECT 1 FROM kalshi_only_alerts WHERE ticker=?",
                  (ticker,)).fetchone():
        alerted_tickers.add(ticker)
        return
    q = quote(m)
    if q is None:
        return
    bid, ask, mid = q
    volume = dec(m.get("volume_24h_fp"))
    if volume is None:
        volume = dec(m.get("volume_24h"))
    if volume is None or volume < MIN_VOLUME:
        return
    if phase == "OPEN":
        # Use the first actually observed Kalshi quote as baseline. The
        # market's open_time can predate the 15-minute trading window, and
        # a worker restart may miss the first minute. Never fabricate a quote.
        rows = db.execute("""SELECT yes_mid, observed_at FROM kalshi_only_samples
            WHERE ticker=? AND observed_at<=?
            ORDER BY observed_at ASC LIMIT 1""",
            (ticker, now - 60)).fetchone()
        if not rows:
            log.info("Waiting for 60s of observed Kalshi quotes for %s", ticker)
            return
        prior = dec(rows[0])
    else:
        rows = db.execute("""SELECT yes_mid FROM kalshi_only_samples
            WHERE ticker=? AND observed_at<=?
            ORDER BY observed_at DESC LIMIT 1""",
            (ticker, now - 60)).fetchone()
        prior = dec(rows[0]) if rows else None
    if prior is None:
        return
    change = mid - prior
    if abs(change) < MIN_MOVE:
        # No strong Kalshi direction; retry on subsequent scans.
        return
    up = change > 0
    direction = "UP" if up else "DOWN"
    entry = ask if up else Decimal(1) - bid
    remaining = max(0, int(close_at - now))
    # Keep learning, accuracy, and model assessment in SQLite/logs, not Telegram.
    log.info("Internal learning %s: %s | %s | %s",
             ticker, learning_summary(db, direction),
             accuracy_report(db), model_assessment(db, direction, change, remaining))
    message = (
        "📊 BTC KALSHI — OPORTUNIDAD A TIEMPO\n"
        + ("🔺 UP" if up else "🔻 DOWN") + " | SEÑAL EXPERIMENTAL\n"
        + "━━━━━━━━━━━━━━━━━━\n"
        + f"📈 YES anterior: ${prior:.3f}\n"
        + f"📈 YES actual: ${mid:.3f}\n"
        + f"📏 Cambio YES: {change:+.3f}\n"
        + f"🎟️ Entrada indicativa {direction}: ${entry:.2f}\n"
        + f"⏳ Tiempo restante: {remaining//60:02d}:{remaining%60:02d}\n"
        + f"🧾 Contrato: {ticker}\n"
        + f"🔎 Tendencia YES: {'subiendo' if up else 'bajando'}; puede revertirse.\n"
        + "⚠️ Señal informativa, no garantiza el resultado."
    )
    if notify(message):
        alerted_tickers.add(ticker)
        db.execute("INSERT OR IGNORE INTO signal_features VALUES (?,?,?,?,?)",
                   (ticker, direction, float(change), remaining, now))
        db.execute("INSERT OR IGNORE INTO kalshi_only_alerts VALUES (?,?,?,?)",
                   (ticker, phase, now, direction))
        log.info("Kalshi-only %s alert: %s %s", phase, ticker, direction)

def check_results(db, now):
    # Use Kalshi official settlement results for completed markets.
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
        predicted = db.execute("""SELECT direction FROM kalshi_only_alerts
            WHERE ticker=? AND phase='OPEN'""", (ticker,)).fetchone()
        if predicted:
            correct = (predicted[0] == "UP" and result == "yes") or (
                predicted[0] == "DOWN" and result == "no")
            db.execute("INSERT OR IGNORE INTO learning_audit VALUES (?,?,?)",
                       (ticker, int(correct), now))
            notify(f"📊 KALSHI — RESULTADO OFICIAL\n🧾 {ticker}\n"
                   f"📈 Dirección de momentum observada: {predicted[0]}\n"
                   f"🏁 Resultado final Kalshi: {result.upper()}\n"
                   f"{'Coincidió' if correct else 'No coincidió'} con el resultado final.\n"
                   "⚠️ La señal mide momentum de cotización, no predice "
                   "el resultado final ni confirma beneficio de compraventa.\n"
                   + accuracy_report(db))

def scan():
    payload = get_json("/markets", {"series_ticker": SERIES, "status": "open", "limit": 100})
    if payload is None:
        return
    now = time.time()
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
                volume = m.get("volume_24h_fp") or m.get("volume_24h")
                db.execute("INSERT OR IGNORE INTO market_observations VALUES (?,?,?,?,?,?)",
                           (ticker, now, str(q[0]), str(q[1]), str(q[2]), str(volume)))
            remaining = closes - now
            # Scan continuously after initial analysis, until the contract closes.
            # OPEN phase is used for one alert per contract.
            if now - opened >= DELAY and remaining >= MIN_REMAINING:
                signal(db, m, now, closes, opened, "OPEN")
        # Check completed markets infrequently to reduce requests.
        if int(now // 300) != int((now - SCAN_SECONDS) // 300):
            check_results(db, now)
        daily_recalibration(db, now)
        retrain_daily(db, now)
        db.execute("DELETE FROM kalshi_only_samples WHERE observed_at < ?", (now - 48 * 3600,))
        db.commit()

def main():
    log.info("Kalshi BTC15M v5.3: daily retraining with temporal holdout")
    while True:
        try:
            scan()
        except Exception:
            log.exception("Scan failed")
        time.sleep(SCAN_SECONDS)

if __name__ == "__main__":
    main()

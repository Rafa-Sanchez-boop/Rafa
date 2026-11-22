"""Kalshi BTC 15-minute market monitor. Alerts only; no trading.

Market prices are NOT independent predictions of BTC direction.
"""
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import requests

BASE = 'https://external-api.kalshi.com/trade-api/v2'
SERIES = os.getenv('KALSHI_SERIES', 'KXBTC15M')
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
DB_PATH = os.getenv('DB_PATH', '/var/data/kalshi_bot.sqlite3')
SCAN_SECONDS = max(15, int(os.getenv('SCAN_SECONDS', '20')))
MAX_SPREAD = Decimal(os.getenv('MAX_SPREAD', '0.04'))
MIN_MOVE = Decimal(os.getenv('MIN_MOVE', '0.025'))
MIN_VOLUME_24H = Decimal(os.getenv('MIN_VOLUME_24H', '100'))
MIN_REMAINING_SECONDS = int(os.getenv('MIN_REMAINING_SECONDS', '90'))
ALERT_COOLDOWN_HOURS = max(0, float(os.getenv('ALERT_COOLDOWN_HOURS', '6')))
TOP_ALERTS = max(1, int(os.getenv('TOP_ALERTS', '15')))
TELEGRAM_TEST_ON_START = os.getenv('TELEGRAM_TEST_ON_START', 'false').lower() in ('1', 'true', 'yes')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('kalshi-btc-monitor')
session = requests.Session()
session.headers.update({'User-Agent': 'KalshiBTC15MMonitor/3.5'})


def dec(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() if value else None
    except (ValueError, AttributeError):
        return None


def db_open():
    os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.execute('CREATE TABLE IF NOT EXISTS snapshots (ticker TEXT NOT NULL, observed_at REAL NOT NULL, midpoint TEXT NOT NULL, PRIMARY KEY(ticker, observed_at))')
    db.execute('CREATE INDEX IF NOT EXISTS idx_snapshots_ticker_time ON snapshots(ticker, observed_at)')
    db.execute('CREATE TABLE IF NOT EXISTS alerts (ticker TEXT PRIMARY KEY, sent_at REAL NOT NULL, side TEXT NOT NULL, entry_price TEXT NOT NULL, reason TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS opening_alerts (ticker TEXT PRIMARY KEY, sent_at REAL NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS final_alerts (ticker TEXT PRIMARY KEY, sent_at REAL NOT NULL)')
    db.commit()
    return db


def fetch_markets():
    for attempt in range(5):
        try:
            response = session.get(f'{BASE}/markets', params={'series_ticker': SERIES, 'status': 'open', 'limit': 100}, timeout=20)
            if response.status_code == 429 or response.status_code >= 500:
                retry = response.headers.get('Retry-After', '')
                delay = min(120, int(retry) if retry.isdigit() else 2 ** (attempt + 1))
                log.warning('HTTP %s; reintento en %ss', response.status_code, delay)
                time.sleep(delay)
                continue
            response.raise_for_status()
            payload = response.json()
            return payload.get('markets', [])
        except (requests.RequestException, ValueError) as exc:
            if attempt == 4:
                raise
            log.warning('Consulta fallida: %s', exc)
            time.sleep(min(2 ** (attempt + 1), 30))
    raise RuntimeError('Kalshi no respondió despuÃ©s de varios intentos')


def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        log.warning('Telegram sin configurar; alerta solo en logs: %s', message)
        return False
    try:
        response = session.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage', json={'chat_id': CHAT_ID, 'text': message}, timeout=20)
        response.raise_for_status()
        data = response.json()
        if not data.get('ok'):
            log.error('Telegram rechazó el mensaje: %s', data.get('description', 'sin detalle'))
        return bool(data.get('ok'))
    except (requests.RequestException, ValueError):
        log.exception('No se pudo enviar alerta Telegram')
        return False


def analyze(market, previous_mid):
    bid = dec(market.get('yes_bid_dollars'))
    ask = dec(market.get('yes_ask_dollars'))
    if bid is None or ask is None or not (0 < bid <= ask < 1):
        return None, 'cotizacion_invalida'
    spread = ask - bid
    if spread > MAX_SPREAD:
        return None, 'spread_alto'
    # Prefer the fixed-point volume field; older API responses may provide volume_24h.
    # Missing volume is unknown, not zero. Never silently bypass the filter.
    volume = dec(market.get('volume_24h_fp'))
    if volume is None:
        volume = dec(market.get('volume_24h'))
    if volume is None:
        return None, 'volumen_no_disponible'
    if volume < MIN_VOLUME_24H:
        return None, 'volumen_bajo'
    mid = (bid + ask) / 2
    if previous_mid is None:
        return None, 'sin_historial_60s'
    change = mid - previous_mid
    if abs(change) < MIN_MOVE:
        return None, 'movimiento_insuficiente'
    side = 'YES' if change > 0 else 'NO'
    entry = ask if side == 'YES' else Decimal(1) - bid
    return {'side': side, 'entry': entry, 'change': change,
            'spread': spread, 'volume': volume}, 'candidato'


_btc_cache = {'at': 0, 'bars': []}


def btc_bars():
    """Recent closed one-minute BTC/USD bars from Kraken; cached 60 seconds."""
    now = time.time()
    if now - _btc_cache['at'] < 60:
        return _btc_cache['bars']
    try:
        response = session.get('https://api.kraken.com/0/public/OHLC',
                               params={'pair': 'XBTUSD', 'interval': 1}, timeout=12)
        response.raise_for_status()
        data = response.json()
        if data.get('error'):
            raise ValueError(str(data['error']))
        key = next(k for k in data['result'] if k != 'last')
        bars = [(int(x[0]), float(x[4])) for x in data['result'][key][:-1]]
        if len(bars) < 180 or now - bars[-1][0] > 180:
            raise ValueError('Insufficient or stale BTC candles')
        _btc_cache.update(at=now, bars=bars)
        return bars
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        log.warning('BTC candles unavailable: %s', exc)
        _btc_cache['at'] = now
        return _btc_cache['bars'] if _btc_cache['bars'] and now - _btc_cache['bars'][-1][0] < 180 else []


def empirical_probability(bars, horizon, strike=None):
    """Experimental analog estimator. Not calibrated or validated for Kalshi."""
    closes = [price for _, price in bars]
    if len(closes) < 180 or horizon < 1:
        return None
    momentum = closes[-1] / closes[-6] - 1
    samples = []
    for i in range(6, len(closes) - horizon):
        past_momentum = closes[i] / closes[i - 5] - 1
        samples.append((abs(past_momentum - momentum), closes[i + horizon] / closes[i] - 1))
    samples.sort(key=lambda x: x[0])
    selected = [ret for _, ret in samples[:80]]
    if len(selected) < 50:
        return None
    target = 0.0 if strike is None else strike / closes[-1] - 1
    # Laplace smoothing. Descriptive historical analog frequency, not calibrated odds.
    probability = (sum(ret > target for ret in selected) + 1) / (len(selected) + 2)
    return probability, len(selected), closes[-1]



def initial_move_target(bars, spot, window=2):
    """Historical 1-minute analogs for the next 2 minutes; NOT tick-level prediction."""
    closes = [p for _, p in bars]
    if len(closes) < 180 or min(closes) <= 0:
        return None
    momentum = closes[-1] / closes[-6] - 1
    analogs = []
    for i in range(6, len(closes) - window):
        past = closes[i] / closes[i - 5] - 1
        move = closes[i + window] / closes[i] - 1
        analogs.append((abs(past - momentum), move))
    analogs.sort(key=lambda x: x[0])
    returns = [ret for _, ret in analogs[:80]]
    if len(returns) < 50:
        return None
    up_count = sum(x > 0 for x in returns)
    down_count = sum(x < 0 for x in returns)
    if max(up_count, down_count) / len(returns) < 0.65:
        return None
    direction = 'UP' if up_count > down_count else 'DOWN'
    matching = sorted(abs(x) for x in returns if (x > 0 if direction == 'UP' else x < 0))
    if len(matching) < 25:
        return None
    # Median observed close-to-close move, not an exact intraperiod extreme.
    median_move = matching[len(matching) // 2]
    target = spot * (1 + median_move if direction == 'UP' else 1 - median_move)
    return direction, target, len(returns), max(up_count, down_count) / len(returns)


def strike_price(market):
    # Never substitute contract bid/ask for BTC settlement strike.
    for field in ('floor_strike', 'strike_price'):
        value = dec(market.get(field))
        if value is not None and value > 1000:
            return float(value)
    return None


def prediction_message(market, now, close_at, phase):
    remaining = int(close_at - now)
    if phase == 'OPEN' and not (810 <= remaining <= 900):
        return None
    if phase == 'FINAL' and not (20 <= remaining <= 180):
        return None
    bars = btc_bars()
    if not bars:
        return None
    strike = strike_price(market)
    if strike is None:
        log.info('%s skipped: no verified BTC strike for %s', phase, market.get('ticker'))
        return None
    horizon = max(1, min(15, round(remaining / 60)))
    estimate = empirical_probability(bars, horizon, strike)
    if estimate is None:
        return None
    probability, count, spot = estimate
    side = 'UP ▲' if probability > 0.5 else 'DOWN ▼'
    confidence = max(probability, 1 - probability)
    if confidence < 0.65:
        log.info('%s skipped: low historical confidence %.3f', phase, confidence)
        return None
    bid = dec(market.get('yes_bid_dollars'))
    ask = dec(market.get('yes_ask_dollars'))
    if bid is None or ask is None or not (0 < bid <= ask < 1):
        return None
    if ask - bid > MAX_SPREAD:
        return None
    entry = ask if probability > 0.5 else 1 - bid
    # Only flag positive theoretical edge versus contract ask.
    if float(entry) + 0.08 >= confidence:
        log.info('%s skipped: insufficient hypothetical edge', phase)
        return None
    phase_name = 'APERTURA' if phase == 'OPEN' else 'ÚLTIMOS 3 MINUTOS'
    quick_note = ''
    if phase == 'OPEN':
        quick = initial_move_target(bars, spot)
        if quick:
            quick_side, quick_target, quick_samples, quick_frequency = quick
            delta = quick_target - spot
            quick_note = (
                f'⚡ KALSHI BITCOIN | PRIMEROS 2 MINUTOS\n'
                f'🚨 MOVIMIENTO INICIAL: {quick_side} {"▼" if quick_side == "DOWN" else "▲"}\n'
                f'💰 Precio inicial aproximado: ${spot:,.2f}\n'
                f'🎯 Precio objetivo estimado: ${quick_target:,.2f}\n'
                f'📏 Movimiento esperado: {delta:+,.2f} USD\n'
                f'⏱️ Ventana estimada: 00:01 a 02:00\n'
                f'📊 Frecuencia histórica de dirección: {quick_frequency:.1%} ({quick_samples} muestras)\n'
                '⚠️ Objetivo estadístico de velas de 1 minuto, no mínimo/máximo exacto.\n'
                '━━━━━━━━━━━━━━━━━━\n'
            )
        else:
            quick_note = '⚡ KALSHI BITCOIN | PRIMEROS 2 MINUTOS: SIN SEÑAL\n━━━━━━━━━━━━━━━━━━\n'

    distance = spot - strike
    distance_label = 'por encima' if distance > 0 else ('por debajo' if distance < 0 else 'igual')
    up_price = float(ask)
    down_price = float(Decimal(1) - bid)
    return (
        f'🔔 SEÑALES BTC 15M | {phase_name}\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'🚨 ALERTA DE CIERRE: {side}\n'
        f'{quick_note}'
        f'📊 UP histórico comparable: {probability:.1%}\n'
        f'📉 DOWN histórico comparable: {1 - probability:.1%}\n'
        f'🔎 Muestras comparables: {count}\n'
        f'💰 Precio BTC (Kraken): ${spot:,.2f}\n'
        f'🎯 Referencia del contrato: ${strike:,.2f}\n'
        f'📏 Distancia: ${abs(distance):,.2f} {distance_label}\n'
        f'🟢 Precio UP estimado: ${up_price:.3f}\n'
        f'🔴 Precio DOWN estimado: ${down_price:.3f}\n'
        f'⏳ Tiempo restante: {remaining // 60:02d}:{remaining % 60:02d}\n'
        f'🧾 Contrato: {market.get("ticker", "")}\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        '⚠️ Señal experimental: frecuencias históricas no calibradas, '
        'NO probabilidades fiables. Cotizaciones indicativas, no garantizadas. '
        'Kraken puede diferir del índice de liquidación de Kalshi. '
        'No es una recomendación de apuesta.'
    )


def scan():
    markets = fetch_markets()
    now = time.time()
    sent = 0
    reasons = {}

    def count(reason):
        reasons[reason] = reasons.get(reason, 0) + 1

    with db_open() as db:
        for market in markets:
            ticker = market.get('ticker', '')
            if not isinstance(ticker, str) or not ticker.startswith(SERIES + '-'):
                count('serie_distinta')
                continue
            close_at = timestamp(market.get('close_time'))
            open_at = timestamp(market.get('open_time'))
            if close_at is None:
                count('sin_cierre')
                continue
            if close_at - now < 20:
                count('poco_tiempo')
                continue
            if open_at and open_at > now:
                count('todavia_no_abierto')
                continue
            if open_at and close_at - open_at > 960:
                count('duracion_invalida')
                continue
            for phase, table in (('OPEN', 'opening_alerts'), ('FINAL', 'final_alerts')):
                if db.execute(f'SELECT 1 FROM {table} WHERE ticker=?', (ticker,)).fetchone():
                    continue
                message = prediction_message(market, now, close_at, phase)
                if message and send_telegram(message):
                    db.execute(f'INSERT INTO {table} VALUES (?,?)', (ticker, now))
                    log.info('Experimental %s signal sent: %s', phase, ticker)
            bid = dec(market.get('yes_bid_dollars'))
            ask = dec(market.get('yes_ask_dollars'))
            if bid is None or ask is None or not (0 < bid <= ask < 1):
                count('cotizacion_invalida')
                continue
            row = db.execute(
                'SELECT midpoint FROM snapshots WHERE ticker=? AND observed_at<=? ORDER BY observed_at DESC LIMIT 1',
                (ticker, now - 60)
            ).fetchone()
            prior = dec(row[0]) if row else None
            midpoint = (bid + ask) / 2
            db.execute('INSERT OR IGNORE INTO snapshots VALUES (?,?,?)',
                       (ticker, now, str(midpoint)))
            count('historial_actualizado')
        db.execute('DELETE FROM snapshots WHERE observed_at < ?', (now - 48 * 3600,))
        # BTC 15m tickers are unique. Keep alert history so a restart never
        # re-sends an old contract; only snapshot history is pruned.
        db.commit()
    details = ', '.join(f'{key}={value}' for key, value in sorted(reasons.items())) or 'sin_detalles'
    log.info('Escaneo: %s mercados, %s alertas | motivos: %s', len(markets), sent, details)


def main():
    log.info('Kalshi BTC15M experimental historical analog signals; no trading')
    if TELEGRAM_TEST_ON_START:
        if send_telegram('🧪 Prueba de conexión Kalshi Bot v3.3: Telegram funciona. No es una señal de mercado.'):
            log.info('Prueba Telegram enviada correctamente')
        else:
            log.error('Prueba Telegram fallida')
    while True:
        try:
            scan()
        except Exception:
            log.exception('Error durante el escaneo')
        time.sleep(SCAN_SECONDS)


if __name__ == '__main__':
    main()
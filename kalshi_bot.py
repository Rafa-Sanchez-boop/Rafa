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
SCAN_SECONDS = max(60, int(os.getenv('SCAN_SECONDS', '60')))
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
session.headers.update({'User-Agent': 'KalshiBTC15MMonitor/3.3'})


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
            if close_at - now < MIN_REMAINING_SECONDS:
                count('poco_tiempo')
                continue
            if open_at and open_at > now:
                count('todavia_no_abierto')
                continue
            if open_at and close_at - open_at > 960:
                count('duracion_invalida')
                continue
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
            signal, reason = analyze(market, prior)
            # Diagnóstico: registrar campos originales y valores calculados.
            # No contiene tokens, chat IDs ni otras credenciales.
            raw_v24 = market.get('volume_24h')
            raw_volume = market.get('volume')
            raw_v24_fp = market.get('volume_24h_fp')
            spread = ask - bid
            movement = midpoint - prior if prior is not None else None
            log.info(
                'DIAG ticker=%s bid=%s ask=%s spread=%s limite_spread=%s '
                'volume_24h=%r volume=%r volume_24h_fp=%r min_volume_24h=%s '
                'mid=%s mid_60s=%s cambio_60s=%s min_move=%s motivo=%s',
                ticker, bid, ask, spread, MAX_SPREAD,
                raw_v24, raw_volume, raw_v24_fp, MIN_VOLUME_24H,
                midpoint, prior, movement, MIN_MOVE, reason
            )
            count(reason)
            if signal is None:
                continue
            if db.execute('SELECT 1 FROM alerts WHERE ticker=?', (ticker,)).fetchone():
                count('ya_alertado')
                continue
            minutes = int((close_at - now) // 60)
            message = (f'📊 KALSHI BTC 15M — MOVIMIENTO DE COTIZACIÓN\n'
                       f'Contrato: {ticker}\nLado observado: {signal["side"]}\n'
                       f'Precio de referencia: ${signal["entry"]:.3f}\n'
                       f'Cambio en precio YES: {signal["change"]:+.3f}\n'
                       f'Spread: ${signal["spread"]:.3f}\nVolumen 24h: {signal["volume"]}\n'
                       f'Tiempo restante: {minutes} min\n\n'
                       'â ï¸ Movimiento observado, no predicciÃ³n validada ni recomendaciÃ³n de compra.')
            if send_telegram(message):
                db.execute('INSERT INTO alerts VALUES (?,?,?,?,?)',
                           (ticker, now, signal['side'], str(signal['entry']), 'contract_quote_momentum'))
                sent += 1
                log.info('Alerta enviada: %s %s', ticker, signal['side'])
                if sent >= TOP_ALERTS:
                    break
            else:
                count('telegram_fallo')
        db.execute('DELETE FROM snapshots WHERE observed_at < ?', (now - 48 * 3600,))
        # BTC 15m tickers are unique. Keep alert history so a restart never
        # re-sends an old contract; only snapshot history is pruned.
        db.commit()
    details = ', '.join(f'{key}={value}' for key, value in sorted(reasons.items())) or 'sin_detalles'
    log.info('Escaneo: %s mercados, %s alertas | motivos: %s', len(markets), sent, details)


def main():
    log.info('Kalshi BTC15M v3.4 — volumen fp y diagnostico, solo alertas')
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
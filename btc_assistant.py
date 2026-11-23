"""Telegram market assistant for BTC/USD and Kalshi BTC 15-minute markets."""
import json
import logging
import math
import os
from datetime import datetime, timezone
from decimal import Decimal
import requests

KALSHI_BASE = "https://external-api.kalshi.com/trade-api/v2"
KRAKEN_TICKER = "https://api.kraken.com/0/public/Ticker"
KRAKEN_OHLC = "https://api.kraken.com/0/public/OHLC"
TELEGRAM_BASE = "https://api.telegram.org/bot"
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.5")
SERIES = os.getenv("KALSHI_SERIES", "KXBTC15M")
log = logging.getLogger("btc-assistant")
session = requests.Session()
session.headers["User-Agent"] = "Rafa-BTC-Market-Assistant/1.0"

COMMANDS = [
    {"command": "ayuda", "description": "Ver comandos del asistente"},
    {"command": "btc", "description": "Precio e indicadores de Bitcoin"},
    {"command": "ciclo", "description": "Ver fase del ciclo corto de BTC"},
    {"command": "mercado", "description": "Ver mercado BTC de Kalshi"},
    {"command": "errores", "description": "Ver aciertos, fallos y objetivos"},
    {"command": "pregunta", "description": "Preguntar a la IA sobre BTC y el mercado"},
]

def _ema(values, period):
    if not values:
        return None
    alpha = 2 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1 - alpha) * result
    return result

def _rsi(closes, period=14):
    if len(closes) <= period:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent = changes[-period:]
    gains = sum(max(x, 0) for x in recent) / period
    losses = sum(max(-x, 0) for x in recent) / period
    if losses == 0:
        return 100.0 if gains else 50.0
    return 100 - (100 / (1 + gains / losses))

def _series(payload):
    result = payload.get("result") or {}
    key = next((k for k in result if k != "last"), None)
    return result.get(key, []) if key else []

def get_snapshot():
    """Fetch a fresh spot, candle, indicator, and Kalshi market snapshot."""
    snap = {"captured_at": datetime.now(timezone.utc).isoformat()}
    try:
        response = session.get(KRAKEN_TICKER, params={"pair": "XBTUSD"}, timeout=10)
        response.raise_for_status()
        payload = response.json()
        item = next(iter(payload.get("result", {}).values()))
        snap["price"] = float(item["c"][0])
        snap["bid"] = float(item["b"][0])
        snap["ask"] = float(item["a"][0])
        snap["high_24h"] = float(item["h"][1])
        snap["low_24h"] = float(item["l"][1])
        snap["volume_24h_btc"] = float(item["v"][1])
        snap["vwap_24h"] = float(item["p"][1])
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError, StopIteration) as exc:
        snap["kraken_error"] = str(exc)

    try:
        response = session.get(KRAKEN_OHLC, params={"pair": "XBTUSD", "interval": 1}, timeout=10)
        response.raise_for_status()
        candles = _series(response.json())
        closes = [float(c[4]) for c in candles if len(c) > 6]
        volumes = [float(c[6]) for c in candles if len(c) > 6]
        highs = [float(c[2]) for c in candles if len(c) > 6]
        lows = [float(c[3]) for c in candles if len(c) > 6]
        if closes:
            snap["trend_5m_pct"] = ((closes[-1] / closes[-6]) - 1) * 100 if len(closes) >= 6 else None
            snap["trend_15m_pct"] = ((closes[-1] / closes[-16]) - 1) * 100 if len(closes) >= 16 else None
            snap["rsi_14_1m"] = _rsi(closes[-100:])
            snap["ema_9_1m"] = _ema(closes[-100:], 9)
            snap["ema_21_1m"] = _ema(closes[-100:], 21)
            ema12 = _ema(closes[-100:], 12)
            ema26 = _ema(closes[-100:], 26)
            snap["macd_1m"] = ema12 - ema26 if ema12 is not None and ema26 is not None else None
            snap["high_20m"] = max(highs[-20:])
            snap["low_20m"] = min(lows[-20:])
            if len(volumes) >= 21 and sum(volumes[-21:-1]) > 0:
                snap["volume_last_minute_vs_prior20"] = volumes[-1] / (sum(volumes[-21:-1]) / 20)
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
        snap["ohlc_error"] = str(exc)

    try:
        response = session.get(KALSHI_BASE + "/markets",
                               params={"series_ticker": SERIES, "status": "open", "limit": 100},
                               timeout=12)
        response.raise_for_status()
        markets = response.json().get("markets", [])
        now = datetime.now(timezone.utc).timestamp()
        active = []
        for market in markets:
            try:
                opened = datetime.fromisoformat(market["open_time"].replace("Z", "+00:00")).timestamp()
                close = datetime.fromisoformat(market["close_time"].replace("Z", "+00:00")).timestamp()
                if opened <= now < close:
                    active.append((close, opened, market))
            except (KeyError, ValueError, TypeError):
                continue
        if active:
            close, opened, market = min(active, key=lambda x: x[0])
            snap["kalshi"] = {
                "ticker": market.get("ticker"),
                "title": market.get("title"),
                "yes_bid": market.get("yes_bid_dollars"),
                "yes_ask": market.get("yes_ask_dollars"),
                "no_bid": market.get("no_bid_dollars"),
                "no_ask": market.get("no_ask_dollars"),
                "volume": market.get("volume"),
                "open_time": market.get("open_time"),
                "close_time": market.get("close_time"),
                "elapsed_minutes": max(0, (now - opened) / 60),
                "remaining_minutes": max(0, (close - now) / 60),
                "seconds_remaining": max(0, int(close - now)),
            }
        else:
            snap["kalshi"] = None
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        snap["kalshi_error"] = str(exc)
    return snap

def _fmt(value, digits=2):
    if value is None:
        return "sin dato"
    try:
        return f"{float(value):,.{digits}f}"
    except (ValueError, TypeError):
        return "sin dato"

def btc_report(snap):
    if "price" not in snap:
        return "No pude obtener el precio actual de BTC en Kraken. Intenta de nuevo en unos segundos."
    rsi = snap.get("rsi_14_1m")
    trend5 = snap.get("trend_5m_pct")
    trend15 = snap.get("trend_15m_pct")
    if trend5 is None or trend15 is None:
        bias = "datos insuficientes para definir una dirección corta"
    elif trend5 > 0 and trend15 > 0 and (rsi is None or rsi < 70):
        bias = "sesgo alcista de corto plazo; todavía puede cambiar"
    elif trend5 < 0 and trend15 < 0 and (rsi is None or rsi > 30):
        bias = "sesgo bajista de corto plazo; todavía puede cambiar"
    else:
        bias = "señales mixtas; no hay dirección clara"
    return (
        "₿ BTC/USD — ANÁLISIS EN VIVO\n"
        f"Precio: USD {_fmt(snap.get('price'))}\n"
        f"Rango 24h: {_fmt(snap.get('low_24h'))}–{_fmt(snap.get('high_24h'))}\n"
        f"Volumen 24h: {_fmt(snap.get('volume_24h_btc'))} BTC | VWAP: USD {_fmt(snap.get('vwap_24h'))}\n"
        f"Cambio 5m / 15m: {_fmt(trend5, 3)}% / {_fmt(trend15, 3)}%\n"
        f"RSI(14): {_fmt(rsi, 1)} | EMA9: {_fmt(snap.get('ema_9_1m'))} | EMA21: {_fmt(snap.get('ema_21_1m'))}\n"
        f"MACD(1m): {_fmt(snap.get('macd_1m'))} | Máx/mín 20m: {_fmt(snap.get('high_20m'))} / {_fmt(snap.get('low_20m'))}\n"
        f"Lectura: {bias}. No es garantía de resultado."
    )

def cycle_report(snap):
    """Describe the observed short-term BTC cycle without assuming a reversal."""
    price = snap.get("price")
    trend5, trend15 = snap.get("trend_5m_pct"), snap.get("trend_15m_pct")
    ema9, ema21 = snap.get("ema_9_1m"), snap.get("ema_21_1m")
    rsi = snap.get("rsi_14_1m")
    high, low = snap.get("high_20m"), snap.get("low_20m")
    if None in (price, trend5, trend15, ema9, ema21):
        return "🌀 BTC — CICLO CORTO\\nFase indeterminada: faltan datos recientes de precio o velas."
    if trend5 > 0 and trend15 > 0 and ema9 > ema21:
        phase = "impulso alcista"
        level = "resistencia"
        boundary = high
    elif trend5 < 0 and trend15 < 0 and ema9 < ema21:
        phase = "impulso bajista"
        level = "soporte"
        boundary = low
    else:
        phase = "transición o rango mixto"
        level = "resistencia/soporte"
        boundary = high if trend5 >= 0 else low
    near = boundary is not None and abs(float(boundary) - float(price)) <= float(price) * 0.001
    if near and phase == "impulso alcista":
        phase += " cerca de resistencia"
    elif near and phase == "impulso bajista":
        phase += " cerca de soporte"
    rsi_text = _fmt(rsi, 1)
    lines = [
        "🌀 BTC — CICLO CORTO",
        f"Fase observada (5–15m): {phase}",
        f"Precio: USD {_fmt(price)} | RSI(14): {rsi_text}",
        f"Variación 5m/15m: {_fmt(trend5, 3)}% / {_fmt(trend15, 3)}%",
    ]
    if boundary is not None:
        lines.append(f"Nivel cercano de {level}: USD {_fmt(boundary)}")
    lines.append("El ciclo orienta el análisis; no asegura cuándo girará el precio.")
    return "\\n".join(lines)

def market_report(snap):
    market = snap.get("kalshi")
    if not market:
        return "No encontré un contrato BTC15M activo de Kalshi en este momento."
    elapsed = int(market["elapsed_minutes"])
    yes_bid, yes_ask = market.get("yes_bid"), market.get("yes_ask")
    return (
        "📊 KALSHI BTC15M — CONTRATO ACTIVO\n"
        f"Contrato: {market.get('ticker')}\n"
        f"Minuto aproximado: {elapsed} | Restan: {market['seconds_remaining']} s\n"
        f"YES bid/ask: {yes_bid or '—'} / {yes_ask or '—'}\n"
        f"NO bid/ask: {market.get('no_bid') or '—'} / {market.get('no_ask') or '—'}\n"
        f"Volumen reportado: {market.get('volume') or '—'}\n"
        "Las cotizaciones son precios de contratos, no el precio de BTC."
    )

def recent_feedback_context(db):
    rows = db.execute("""SELECT source, predicted_direction, actual_direction,
                                target_price, high_after_signal, low_after_signal,
                                target_reached, note
                         FROM kalshi_prediction_feedback
                         WHERE status IN ('evaluated','reported')
                         ORDER BY rowid DESC LIMIT 8""").fetchall()
    if not rows:
        return "Todavía no hay resultados guardados."
    return "\n".join(
        f"- {source}: {predicted or 'sin dirección'} → {actual or 'pendiente'}; "
        f"objetivo={target or 'sin dato'}, máximo={high or 'sin dato'}, "
        f"mínimo={low or 'sin dato'}, objetivo_tocado={reached if reached is not None else 'sin dato'}; "
        f"{note or ''}"
        for source, predicted, actual, target, high, low, reached, note in rows)

def feedback_report(db):
    evaluated = db.execute("""SELECT predicted_direction, actual_direction,
                                      target_reached
                               FROM kalshi_prediction_feedback
                               WHERE status='evaluated' AND source='bot_forecast'
                               ORDER BY evaluated_at DESC LIMIT 20""").fetchall()
    reported = db.execute("""SELECT COUNT(*) FROM kalshi_prediction_feedback
                             WHERE status='reported'""").fetchone()[0]
    if not evaluated and not reported:
        return "Aún no hay resultados evaluados ni errores reportados guardados."
    correct = sum(predicted == actual for predicted, actual, _ in evaluated)
    target_rows = [row[2] for row in evaluated if row[2] is not None]
    touched = sum(bool(value) for value in target_rows)
    lines = ["🧠 RAFA — HISTORIAL DE RESULTADOS"]
    if evaluated:
        lines.append(f"Últimas {len(evaluated)} evaluaciones: {correct}/{len(evaluated)} direcciones coincidieron.")
    else:
        lines.append("Aún no hay evaluaciones automáticas completas.")
    if target_rows:
        lines.append(f"Primer objetivo tocado: {touched}/{len(target_rows)}.")
    lines.append(f"Errores reportados por ti: {reported}.")
    for predicted, actual, reached in evaluated[:5]:
        status = "acertó al cierre" if predicted == actual else "falló al cierre"
        target = "objetivo tocado" if reached == 1 else "objetivo no tocado" if reached == 0 else "objetivo sin dato"
        lines.append(f"{predicted} → {actual}: {status}; {target}.")
    return "\n".join(lines)

def ai_answer(question, snap, feedback_context=""):
    if not OPENAI_API_KEY:
        return ("La conexión con la IA conversacional aún no está activada. "
                "Puedo mostrar el análisis técnico en /btc y el contrato actual en /mercado. "
                "Para responder preguntas libres, falta guardar OPENAI_API_KEY como secreto en Render.")
    system = (
        "Eres Rafa, un asistente de análisis de Bitcoin y del mercado BTC de Kalshi. "
        "Responde en español sencillo y breve. Usa el bloque JSON de datos actuales para precios, "
        "indicadores y contrato; no inventes datos ni digas que los tienes si faltan. "
        "Analiza los ciclos de corto plazo (impulso, retroceso, rango y transición) con tendencia, medias, RSI y niveles cercanos. " "Trata el ciclo como marco de lectura, no como ley: no afirmes que una subida debe caer de inmediato ni que una caída debe rebotar de inmediato; las tendencias pueden continuar y el momento del giro es incierto. " "Limita la lectura al horizonte de los datos disponibles. " "Si preguntan por un minuto específico del contrato, compara ese minuto con elapsed_minutes "
        "y seconds_remaining y explica el horizonte. Da un sesgo condicionado (alcista, bajista o mixto), "
        "las señales que lo respaldan y qué lo invalidaría. Nunca prometas certeza, ganancias ni 90/99%. "
        "No afirmes que una operación se hizo. Solo calcula escenarios de contratos si el usuario indica "
        "cantidad, lado, precio o los datos necesarios; no supongas cantidad. Si faltan precio de entrada, "
        "lado o cantidad, pregunta cuál falta. Las comisiones de Kalshi no están incluidas salvo que se "
        "proporcionen. Si consultan noticias, datos on-chain o liquidaciones y esos datos no aparecen en "
        "el JSON, dilo claramente y no los inventes. Usa el historial de errores como advertencias, "
        "separa los reportes manuales de resultados automáticos y no afirmes que el sistema se "
        "reentrenó solo. Esto es análisis informativo, no asesoría financiera."
    )
    try:
        response = session.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": "Bearer " + OPENAI_API_KEY},
            json={"model": OPENAI_MODEL, "instructions": system,
                  "input": "DATOS DE MERCADO (capturados ahora):\n"
                           + json.dumps(snap, ensure_ascii=False)
                           + "\n\nERRORES Y RESULTADOS RECIENTES:\n"
                           + (feedback_context or "Sin historial disponible.")
                           + "\n\nPREGUNTA DEL USUARIO: " + question,
                  "max_output_tokens": 350},
            timeout=25)
        response.raise_for_status()
        payload = response.json()
        for item in payload.get("output", []):
            if item.get("type") == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text" and part.get("text"):
                        return part["text"][:3900]
        return "La IA no devolvió texto para esta pregunta. Prueba otra vez."
    except (requests.RequestException, ValueError, KeyError, TypeError):
        log.exception("OpenAI market assistant request failed")
        return "No pude obtener respuesta de la IA ahora. Revisa la clave de API y vuelve a intentar."

def help_text():
    return (
        "🤖 RAFA — COMANDOS\n"
        "/btc — precio BTC e indicadores: RSI, EMA, MACD, volumen y rangos\n"
        "/ciclo — fase observada del ciclo corto y niveles\n"
        "/mercado — contrato BTC15M activo de Kalshi\n"
        "/errores — ver aciertos, fallos y objetivos\n"
        "/pregunta tu pregunta — consultar la IA con datos actuales\n"
        "También puedes escribir una pregunta normal en el chat.\n"
        "No se fija una cantidad de contratos: inclúyela solo si quieres calcular un escenario."
    )

def register_commands():
    if not TOKEN:
        return
    try:
        response = session.post(TELEGRAM_BASE + TOKEN + "/setMyCommands",
                                json={"commands": COMMANDS}, timeout=10)
        response.raise_for_status()
        if not response.json().get("ok"):
            log.warning("Telegram did not accept command menu")
    except (requests.RequestException, ValueError):
        log.exception("Could not register Telegram command menu")

def process_updates(db):
    """Receive Telegram text from the configured chat and answer market questions."""
    if not TOKEN or not CHAT_ID:
        return
    row = db.execute("SELECT value FROM telegram_state WHERE key='update_offset'").fetchone()
    offset = int(row[0]) if row else None
    params = {"timeout": 0, "allowed_updates": json.dumps(["message"])}
    if offset is not None:
        params["offset"] = offset
    try:
        response = session.get(TELEGRAM_BASE + TOKEN + "/getUpdates",
                               params=params, timeout=10)
        response.raise_for_status()
        updates = response.json().get("result", [])
    except requests.HTTPError as exc:
        if "409" in str(exc):
            log.error("Telegram getUpdates conflict: a webhook may be configured; commands need polling.")
        else:
            log.warning("Telegram getUpdates failed: %s", exc)
        return
    except (requests.RequestException, ValueError):
        log.exception("Telegram update polling failed")
        return

    for update in updates:
        update_id = update.get("update_id")
        message = update.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        text = message.get("text", "")
        if update_id is None:
            continue
        try:
            if chat_id == CHAT_ID and isinstance(text, str) and text.strip():
                command = text.strip().split(None, 1)[0].split("@", 1)[0].lower()
                question = text.strip().split(None, 1)[1] if len(text.strip().split(None, 1)) > 1 else ""
                if command in ("/start", "/ayuda", "/help"):
                    reply = help_text()
                elif command == "/btc":
                    reply = btc_report(get_snapshot())
                elif command == "/ciclo":
                    reply = cycle_report(get_snapshot())
                elif command == "/mercado":
                    reply = market_report(get_snapshot())
                elif command == "/errores":
                    reply = feedback_report(db)
                elif command == "/pregunta":
                    if not question:
                        reply = "Escribe: /pregunta ¿Bitcoin seguirá subiendo en el minuto 8?"
                    else:
                        snap = get_snapshot()
                        reply = ai_answer(question, snap, recent_feedback_context(db))
                elif not text.strip().startswith("/"):
                    snap = get_snapshot()
                    reply = ai_answer(text.strip(), snap, recent_feedback_context(db))
                else:
                    reply = "No reconozco ese comando. Usa /ayuda."
                send_message(chat_id, reply)
        except Exception:
            log.exception("Could not answer Telegram update %s", update_id)
            send_message(chat_id, "No pude procesar esa consulta. Prueba /btc o /ayuda.")
        db.execute("""INSERT INTO telegram_state(key,value) VALUES ('update_offset',?)
                      ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                   (str(int(update_id) + 1),))
        db.commit()

def send_message(chat_id, message):
    if not TOKEN:
        return False
    try:
        response = session.post(
            TELEGRAM_BASE + TOKEN + "/sendMessage",
            json={"chat_id": chat_id, "text": message[:4000]},
            timeout=15)
        response.raise_for_status()
        return bool(response.json().get("ok"))
    except (requests.RequestException, ValueError):
        log.exception("Telegram assistant reply failed")
        return False

# KALSHI AI BOT v2 — nube + Telegram (solo alertas)

## Qué hace
- Escanea mercados abiertos con el endpoint público documentado de Kalshi.
- Filtra por volumen 24h y spread.
- Compara el precio medio actual con el último escaneo y genera alertas de movimiento.
- Envía alertas opcionales por Telegram.
- Guarda observaciones y evita repetir la misma alerta durante un periodo configurable.
- **No tiene código de compra/venta, no usa claves de trading y no coloca órdenes.**

## Asistente de Bitcoin en Telegram
El mismo bot admite estos comandos en el chat configurado:

- `/ayuda` — muestra el menú de comandos.
- `/btc` — consulta precio, rango y volumen de 24 horas, cambio de 5 y 15 minutos, RSI, EMA 9/21, MACD y niveles recientes.
- `/mercado` — consulta el contrato BTC15M activo en Kalshi, sus precios YES/NO y el tiempo restante.
- `/pregunta ¿En el minuto 8 seguirá subiendo?` — responde con la IA y una captura actual de Kraken y Kalshi.
- También puedes escribir una pregunta normal en el chat.

Para habilitar respuestas de lenguaje libre, configura `OPENAI_API_KEY` como secreto del servicio en Render. El modelo predeterminado es `gpt-5.5`; se puede cambiar con `OPENAI_MODEL`. Sin esa clave, `/btc` y `/mercado` siguen disponibles y el bot explica que la IA conversacional necesita configuración.

La IA puede analizar los datos que recibe del mercado, pero no puede garantizar la dirección futura. No coloca órdenes. Si preguntas por un cálculo de contratos, indica la cantidad, el lado y el precio de entrada; el bot no supone una cantidad ni incluye comisiones si no tiene esos datos.

## Límite honesto del modelo
Las señales y el análisis técnico no aseguran el resultado de Bitcoin ni una ganancia. Los indicadores describen datos recientes y pueden cambiar con rapidez. Las noticias y métricas on-chain no se presentan como datos en vivo, a menos que se conecte una fuente para ellas.

## Despliegue recomendado: Render Background Worker
1. Crea un repositorio privado en GitHub y sube estos archivos.
2. En Render, elige **New + → Blueprint** y conecta el repositorio; Render leerá `render.yaml`.
3. Confirma el servicio `kalshi-ai-alerts`. El blueprint configura un worker Docker, un disco persistente `/var/data` y variables iniciales.
4. En el panel del servicio, abre **Environment** y añade:
   - `TELEGRAM_BOT_TOKEN` = token del bot que crees con `@BotFather`
   - `TELEGRAM_CHAT_ID` = tu chat ID numérico
5. Guarda y despliega. Revisa **Logs**: debe aparecer `KALSHI AI BOT v2 iniciado`.
6. Para detenerlo, suspende o elimina el Background Worker desde Render.

El servicio con disco persistente puede requerir un plan de pago. Render documenta que el filesystem por defecto es efímero y que los discos persistentes están disponibles para background workers en planes compatibles.

## Configurar Telegram
1. En Telegram, busca `@BotFather`, inicia el chat y usa `/newbot`.
2. Sigue las instrucciones y copia el token. No lo publiques ni lo compartas.
3. Abre el chat con tu nuevo bot y pulsa Start.
4. Para obtener tu chat ID, abre `https://api.telegram.org/bot<TU_TOKEN>/getUpdates` en el navegador después de enviarle un mensaje al bot. Busca `message.chat.id`.
5. Añade ambos valores como variables secretas en Render. Nunca los pongas en el código ni en GitHub.

## Variables opcionales
- `SCAN_SECONDS=300` (mínimo efectivo 60)
- `MIN_VOLUME_24H=100`
- `MAX_SPREAD=0.04`
- `MIN_MOVE=0.025`
- `ALERT_COOLDOWN_HOURS=6`
- `MAX_PAGES=5`
- `TOP_ALERTS=15`
- `DB_PATH=/var/data/kalshi_bot.sqlite3`

## Fuentes
- API pública: `https://external-api.kalshi.com/trade-api/v2`
- Documentación: https://docs.kalshi.com/getting_started/quick_start_market_data
- Render: https://render.com/docs/blueprint-spec y https://render.com/docs/disks
- Telegram Bot API: https://core.telegram.org/bots/api

## Seguridad
No agregues claves privadas de Kalshi: este proyecto solo lee datos públicos. No hay operaciones automatizadas.

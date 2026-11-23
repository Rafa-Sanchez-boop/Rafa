# Evaluación preliminar del pronóstico al abrir KXBTC15M

## Alcance

- Datos públicos de Kalshi: 80 contratos KXBTC15M ya cerrados, del 22 de noviembre de 2026 10:30 UTC al 23 de noviembre de 2026 06:15 UTC.
- Para cada contrato se usó la primera vela de un minuto y el resultado oficial YES/NO.
- Corte temporal: 23 de noviembre de 2026 06:41 UTC. No se usaron cierres posteriores al corte.
- División cronológica: primeras 56 ventanas para ajustar; últimas 24 para evaluar.
- Este es un estudio exploratorio pequeño, no una validación de rentabilidad.

## Resultados

| Regla exploratoria | Señales en holdout | Aciertos | Brier |
|---|---:|---:|---:|
| Calibración por precio YES en bandas de 10¢; alertar si p ≥ 58% o p ≤ 42% | 11 | 54.5% | 0.2362 |
| Bandas YES de 20¢ + signo del retorno Kraken en los 15 minutos previos; mismo umbral | 12 | 33.3% | 0.3054 |

El promedio YES de entrenamiento fue 55.4%; en el holdout, 37.5%. El cambio entre periodos y el tamaño de muestra muestran que las estimaciones son inestables. La segunda regla no mejoró la primera.

## Decisión

No conectar todavía una probabilidad generada por estas reglas al bot ni activar un precio objetivo de compra/venta. Los resultados no respaldan una señal fiable ni demuestran beneficio después del spread, comisiones y ejecución. El estudio de velas de un minuto también solo permite evaluar una señal alrededor del cierre del primer minuto, no una señal instantánea al abrir.

Siguiente trabajo: ampliar el histórico, hacer evaluación walk-forward con más ventanas y comparar contra la probabilidad implícita en el precio YES del propio mercado. Solo añadir alertas probabilísticas si la precisión y la calibración mejoran de forma estable; el bot seguirá sin enviar órdenes.

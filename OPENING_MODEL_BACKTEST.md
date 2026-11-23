# Evaluación del pronóstico al abrir KXBTC15M

## Alcance y método

- Se consultaron 360 contratos KXBTC15M cerrados y sus primeras velas de un minuto.
- Se estimó la probabilidad de YES usando solo el precio YES de Kalshi en bandas fijas de 10¢ y resultados anteriores; se aplicó suavizado estadístico y se exigieron al menos 8 casos por banda.
- Umbral fijo de alerta: probabilidad estimada de UP ≥65% o ≤35% para DOWN.
- Evaluación walk-forward: cada bloque se probó con ventanas posteriores a las usadas para ajustarlo. No se usaron datos de cierre futuros para la predicción.
- La señal ocurre alrededor del cierre de la primera vela de un minuto, no instantáneamente al abrir.

## Resultados walk-forward

| Bloque | Entrenamiento | Prueba | Señales | Aciertos | Brier | Retorno bruto estimado |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 180 | 61 | 23 | 69.6% | 0.2282 | +2.6% por contrato |
| 2 | 241 | 61 | 15 | 73.3% | 0.2344 | +6.5% por contrato |
| 3 | 302 | 58 | 15 | 66.7% | 0.2393 | −0.2% por contrato |
| Total de prueba | — | 180 | 53 | 69.8% | — | — |

El retorno es una estimación bruta al comprar al precio de entrada observado y mantener hasta la liquidación. No descuenta comisiones y no evalúa la venta rápida que pidió el usuario. El tercer bloque tuvo retorno bruto negativo.

## Comparación con Kraken

Una prueba exploratoria anterior combinó bandas de precio YES de 20¢ con el signo del retorno Kraken de los 15 minutos anteriores. En 12 señales de prueba acertó 4 (33.3%) y no mejoró el resultado de Kalshi por sí solo. No se incluye Kraken en la regla evaluada arriba.

## Decisión

La calibración de Kalshi sola merece más evaluación, pero aún no demuestra una estrategia de compra y venta rápida rentable: 53 señales es una muestra limitada, un bloque terminó en negativo antes de comisiones y no se ha probado una salida anticipada. No conectar todavía esta regla al bot ni desplegarla en Render.

Siguiente trabajo: backtestear objetivos de salida anticipada con precios bid/ask de las velas, comisiones aplicables y ventanas de prueba nuevas. Solo integrar alertas si la precisión y el retorno neto se sostienen en varios bloques. El bot seguirá en modo de predicciones y sin órdenes automáticas.

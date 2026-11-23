# Evaluación del pronóstico al abrir KXBTC15M

## Alcance y método

- Se consultaron 360 contratos KXBTC15M cerrados y sus velas de un minuto.
- Se estimó la probabilidad de YES usando solo el precio YES de Kalshi en bandas fijas de 10¢ y resultados anteriores; se aplicó suavizado estadístico y se exigieron al menos 8 casos por banda.
- Umbral fijo de alerta: probabilidad estimada de UP ≥65% o ≤35% para DOWN.
- Evaluación walk-forward: cada bloque se probó con ventanas posteriores a las usadas para ajustarlo. No se usaron datos de cierre futuros para la predicción.
- La señal ocurre alrededor del cierre de la primera vela de un minuto, no instantáneamente al abrir.

## Resultados de dirección

| Bloque | Entrenamiento | Prueba | Señales | Aciertos | Brier | Retorno bruto estimado al mantener hasta cierre |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 180 | 61 | 23 | 69.6% | 0.2282 | +2.6% por contrato |
| 2 | 241 | 61 | 15 | 73.3% | 0.2344 | +6.5% por contrato |
| 3 | 302 | 58 | 15 | 66.7% | 0.2393 | −0.2% por contrato |
| Total de prueba | — | 180 | 53 | 69.8% | — | — |

El retorno bruto al mantener hasta el cierre no incluye comisiones. El tercer bloque ya fue negativo antes de comisiones.

## Prueba de salida rápida

En las mismas 53 señales se simuló comprar al ask del primer minuto y vender al bid cuando se alcanzaba el objetivo o el stop; si no ocurría ninguno, se usó la última cotización antes del cierre. Se descontó una comisión taker de entrada y otra de salida por contrato con la fórmula general publicada por Kalshi: tarifa = redondeo hacia arriba de 0.07 × precio × (1 − precio). La [tarifa oficial puede variar por producto y fecha](https://kalshi.com/docs/kalshi-fee-schedule.pdf).

| Objetivo | Stop | Señales | Objetivos alcanzados | Operaciones netas positivas | Ganancia neta media por contrato | Resultado total |
|---:|---:|---:|---:|---:|---:|---:|
| +3¢ | −5¢ | 53 | 24 | 43.4% | −7.08¢ | −$3.75 |
| +3¢ | −10¢ | 53 | 29 | 49.1% | −7.55¢ | −$4.00 |
| +5¢ | −5¢ | 53 | 23 | 43.4% | −7.13¢ | −$3.78 |
| +5¢ | −10¢ | 53 | 29 | 54.7% | −6.85¢ | −$3.63 |
| +10¢ | −5¢ | 53 | 21 | 39.6% | −6.54¢ | −$3.46 |
| +10¢ | −10¢ | 53 | 27 | 50.9% | −6.20¢ | −$3.29 |

Esta simulación usa cotizaciones de velas, no ejecuciones reales: no confirma profundidad disponible, llenado completo, latencia ni deslizamiento. Es una aproximación y puede ser más favorable que una operación real.

## Comparación con Kraken

Una prueba exploratoria anterior combinó bandas de precio YES de 20¢ con el signo del retorno Kraken de los 15 minutos previos. En 12 señales de prueba acertó 4 (33.3%) y no mejoró el resultado de Kalshi por sí solo. No se incluye Kraken en la regla evaluada arriba.

## Decisión

La regla tuvo cerca de 70% de aciertos direccionales en estas 53 señales, pero eso no se tradujo en una salida rápida rentable: las seis combinaciones de objetivo y stop quedaron negativas después de spread y comisiones. No conectar este modelo ni sus precios de entrada/salida al bot ni desplegarlo en Render todavía.

Siguiente trabajo: buscar otra señal y evaluarla en bloques nuevos con cotizaciones de compra/venta y comisiones. Solo integrar alertas probabilísticas si la dirección y el retorno neto se sostienen de forma estable. El bot seguirá en modo de predicciones y sin órdenes automáticas.

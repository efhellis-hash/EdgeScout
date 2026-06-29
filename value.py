"""
value.py — Deteccion de valor en MODO MERCADO (line shopping puro).

Cambio de filosofia (2026-06): el EV ya NO se calcula contra la opinion de un
modelo (Haiku). Se calcula contra el CONSENSO DEL MERCADO SHARP.

La idea: el consenso sin vig (ancla Pinnacle/mediana) es la mejor estimacion de
la probabilidad real. Hay valor cuando la MEJOR cuota disponible entre todas las
casas te paga COMO SI el equipo fuera menos probable de lo que el consenso dice.
Esa brecha es edge real y medible (se confirma con CLV), no una corazonada.

  EV = market_fair_prob * (mejor_decimal - 1) - (1 - market_fair_prob)

No hay "divergencia de modelo" que vigilar: aqui no hay modelo propio. El unico
filtro es que el EV supere el minimo. Edge delgado pero honesto.
"""
from config import EDGE_MINIMO


def expected_value(market_fair_prob: float, decimal_odds: float) -> float:
    """EV por unidad apostada en modo mercado.
    market_fair_prob: prob justa de consenso sharp (la 'verdad' del mercado).
    decimal_odds: la MEJOR cuota disponible (lo que de verdad te pagan)."""
    return market_fair_prob * (decimal_odds - 1) - (1 - market_fair_prob)


def evaluate(market_fair_prob: float, decimal_odds: float) -> dict:
    """Decide si hay valor accionable. En modo mercado el unico criterio es que
    la mejor cuota disponible pague por encima de lo justo (consenso sharp).

    Firma nueva: evaluate(market_fair_prob, decimal_odds). Ya NO recibe model_prob
    porque no hay modelo. Mantiene la llave 'ev' y 'veredicto' que espera analyst."""
    ev = expected_value(market_fair_prob, decimal_odds)

    if ev < EDGE_MINIMO:
        veredicto = "RECHAZO_EV_BAJO"
        motivo = (f"EV {ev:.1%} por debajo del minimo {EDGE_MINIMO:.0%}. "
                  "El mejor precio no supera al consenso del mercado.")
    else:
        veredicto = "PASO"
        motivo = (f"EV {ev:.1%}: la mejor cuota paga por encima del consenso "
                  "sharp. Valor de line shopping.")

    return {
        "veredicto": veredicto,
        "ev": round(ev, 4),
        "market_fair": round(market_fair_prob, 4),
        "motivo": motivo,
    }

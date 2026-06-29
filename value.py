"""
value.py — Deteccion de valor en MODO MERCADO (line shopping puro).

El EV se calcula contra el CONSENSO DEL MERCADO (prob justa de las OTRAS casas),
no contra un modelo. Hay valor cuando la MEJOR cuota disponible paga por encima
de lo que el consenso considera justo:

  EV = market_fair_prob * (mejor_decimal - 1) - (1 - market_fair_prob)

DOS filtros, ambos obligatorios:
  1. EV >= EDGE_MINIMO  -> el precio supera al consenso por un margen real.
  2. n_books >= MIN_CASAS -> el consenso tiene muestra suficiente. Un "consenso"
     de pocas casas es ruido: un +EV ahi probablemente es una linea rezagada o un
     outlier, no edge. Este filtro es lo que separa senal de basura.
"""
from config import EDGE_MINIMO, MIN_CASAS


def expected_value(market_fair_prob: float, decimal_odds: float) -> float:
    """EV por unidad apostada en modo mercado."""
    return market_fair_prob * (decimal_odds - 1) - (1 - market_fair_prob)


def evaluate(market_fair_prob: float, decimal_odds: float,
             n_books: int = 0) -> dict:
    """Decide si hay valor accionable. Firma: (market_fair_prob, decimal_odds,
    n_books). El n_books viene de best_moneyline (tamano del consenso)."""
    ev = expected_value(market_fair_prob, decimal_odds)

    if n_books < MIN_CASAS:
        veredicto = "RECHAZO_POCA_MUESTRA"
        motivo = (f"Consenso de solo {n_books} casa(s) (<{MIN_CASAS}). "
                  "Muestra insuficiente: cualquier EV aqui es ruido, no edge.")
    elif ev < EDGE_MINIMO:
        veredicto = "RECHAZO_EV_BAJO"
        motivo = (f"EV {ev:.1%} por debajo del minimo {EDGE_MINIMO:.0%}. "
                  "El mejor precio no supera al consenso por margen suficiente.")
    else:
        veredicto = "PASO"
        motivo = (f"EV {ev:.1%} sobre consenso de {n_books} casas. "
                  "Valor de line shopping real.")

    return {
        "veredicto": veredicto,
        "ev": round(ev, 4),
        "market_fair": round(market_fair_prob, 4),
        "n_books": n_books,
        "motivo": motivo,
    }

"""
value.py — Deteccion de valor con humildad de calibracion.

Aqui es donde casi todos se enganan. Que tu modelo discrepe del mercado NO es
senal de valor: lo mas probable es que tu modelo este mal. Este modulo trata la
divergencia grande como SOSPECHA, no como oportunidad, y solo deja pasar EV real
sobre las cuotas que de verdad te pagan (con vig).
"""
from config import EDGE_MINIMO, DIVERGENCIA_MAX


def expected_value(model_prob: float, decimal_odds: float) -> float:
    """EV por unidad apostada, usando la cuota OFRECIDA (con vig) — que es lo
    que realmente cobras — y tu probabilidad estimada."""
    return model_prob * (decimal_odds - 1) - (1 - model_prob)


def evaluate(model_prob: float, market_fair_prob: float,
             decimal_odds: float) -> dict:
    """Decide si hay valor accionable o si es ruido/error de modelo."""
    divergencia = abs(model_prob - market_fair_prob)
    ev = expected_value(model_prob, decimal_odds)
    edge_prob = model_prob - market_fair_prob  # ventaja en prob justa

    veredicto = "PASO"   # PASO = recomendable; resto = no
    motivo = ""

    if divergencia > DIVERGENCIA_MAX:
        veredicto = "RECHAZO_DIVERGENCIA"
        motivo = (f"Divergencia {divergencia:.1%} > {DIVERGENCIA_MAX:.0%}. "
                  "Probable error de modelo, no valor. Desconfía de ti mismo.")
    elif ev < EDGE_MINIMO:
        veredicto = "RECHAZO_EV_BAJO"
        motivo = f"EV {ev:.1%} por debajo del minimo {EDGE_MINIMO:.0%}."
    else:
        motivo = f"EV {ev:.1%} con divergencia razonable {divergencia:.1%}."

    return {
        "veredicto": veredicto,
        "ev": round(ev, 4),
        "edge_prob": round(edge_prob, 4),
        "divergencia": round(divergencia, 4),
        "motivo": motivo,
    }

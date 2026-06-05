"""
bankroll.py — Gestion de banca. Recomienda monto, nunca ejecuta.

Kelly fraccionado (cuarto de Kelly) con tope duro. Sin martingala, sin perseguir
perdidas. Si se toca el limite de perdida diaria, el agente deja de recomendar.
"""
from config import KELLY_FRACCION, STAKE_MAX_PCT, LIMITE_PERDIDA_DIARIA


def kelly_stake(model_prob: float, decimal_odds: float) -> float:
    """Fraccion de banca segun Kelly fraccionado, con tope. 0 si no hay edge."""
    b = decimal_odds - 1
    if b <= 0:
        return 0.0
    f_full = (model_prob * b - (1 - model_prob)) / b   # Kelly completo
    if f_full <= 0:
        return 0.0
    f = f_full * KELLY_FRACCION
    return round(min(f, STAKE_MAX_PCT), 4)


def stake_usd(bankroll: float, model_prob: float, decimal_odds: float) -> dict:
    frac = kelly_stake(model_prob, decimal_odds)
    return {
        "stake_pct": frac,
        "stake_usd": round(bankroll * frac, 2),
        "nota": "Recomendacion. Tu ejecutas. Nunca subas el monto para recuperar.",
    }


def puede_operar(perdida_acumulada_hoy: float, bankroll: float) -> bool:
    """False si ya se toco el limite de perdida diaria."""
    return perdida_acumulada_hoy < bankroll * LIMITE_PERDIDA_DIARIA

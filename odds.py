"""
odds.py — Lineas del mercado + matematica honesta de probabilidad.

Aqui esta el baseline que NINGUN modelo debe ignorar: la probabilidad
implicita SIN vig del mercado. Es lo que el mercado realmente cree, y es
brutalmente dificil de superar. Tu "modelo" arranca desde aqui, no desde cero.
"""
import requests
from config import THE_ODDS_API_KEY, SPORT_KEYS, MERCADOS, REGIONES


# ---- Conversion de cuotas -------------------------------------------------
def american_to_decimal(american: int) -> float:
    if american > 0:
        return american / 100 + 1
    return 100 / abs(american) + 1


def implied_prob(decimal_odds: float) -> float:
    """Probabilidad implicita CON vig (lo que cuelga la casa)."""
    return 1 / decimal_odds


def devig_two_way(p1: float, p2: float):
    """Quita el vig de un mercado de dos vias (metodo proporcional).
    Devuelve las probabilidades 'justas' que suman 1.0."""
    total = p1 + p2
    return p1 / total, p2 / total


# ---- Descarga de lineas ---------------------------------------------------
def fetch_odds(sport: str) -> list:
    """Trae odds actuales (ML, spread, totales) para todos los juegos del dia.
    Devuelve la estructura cruda de The Odds API."""
    if not THE_ODDS_API_KEY:
        raise RuntimeError("THE_ODDS_API_KEY no configurada")
    sport_key = SPORT_KEYS[sport]
    r = requests.get(
        f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds",
        params={
            "apiKey": THE_ODDS_API_KEY,
            "regions": REGIONES,
            "markets": ",".join(MERCADOS),
            "oddsFormat": "american",
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def best_moneyline(game: dict):
    """De todas las casas, toma la MEJOR cuota disponible por equipo
    (clave para CLV: apostar al mejor precio) y la prob justa sin vig."""
    teams = {}
    for book in game.get("bookmakers", []):
        for market in book.get("markets", []):
            if market["key"] != "h2h":
                continue
            for outcome in market["outcomes"]:
                name = outcome["name"]
                price = outcome["price"]
                dec = american_to_decimal(price)
                if name not in teams or dec > teams[name]["decimal"]:
                    teams[name] = {"american": price, "decimal": dec,
                                   "book": book["title"]}
    if len(teams) != 2:
        return None
    (a, da), (b, db) = [(n, d) for n, d in teams.items()]
    pa, pb = implied_prob(da["decimal"]), implied_prob(db["decimal"])
    fair_a, fair_b = devig_two_way(pa, pb)
    return {
        a: {**da, "fair_prob": round(fair_a, 4)},
        b: {**db, "fair_prob": round(fair_b, 4)},
        "matchup": f"{game.get('away_team')} @ {game.get('home_team')}",
        "commence_time": game.get("commence_time"),
    }

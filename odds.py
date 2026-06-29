"""
odds.py — Lineas del mercado + matematica honesta de probabilidad.

MODO MERCADO / LINE SHOPPING (correccion 2026-06):
  El error que se arregla: antes se devigaba y se comparaba contra la MISMA casa,
  asi que el vig nunca se iba y TODO daba ~-2.3% (el vig promedio). Imposible
  encontrar valor: comparabas una casa contra si misma.

  La forma correcta del line shopping:
    - prob justa = CONSENSO de las OTRAS casas (excluyendo la que da el mejor
      precio para ese equipo). Esa es la "verdad" del mercado.
    - precio que apuestas = el MEJOR precio individual disponible.
    - hay edge cuando UNA casa se sale del consenso y te paga de mas.
  Asi el fair y el precio NUNCA salen de la misma fuente, y el vig deja de
  contaminar el EV.

  Tambien expone n_books (cuantas casas formaron el consenso) para que value.py
  exija una muestra minima: un "consenso" de 2 casas no es consenso, es ruido.
"""
import statistics
import requests
from config import THE_ODDS_API_KEY, SPORT_KEYS, MERCADOS, REGIONES

# Casas afiladas: si alguna esta presente, su linea (ya sin vig) es mejor ancla.
# Con REGIONES='us' normalmente NO aparece Pinnacle; el sistema cae al consenso.
SHARP_BOOKS = ("pinnacle", "circa", "betonline")


# ---- Conversion de cuotas -------------------------------------------------
def american_to_decimal(american: int) -> float:
    if american > 0:
        return american / 100 + 1
    return 100 / abs(american) + 1


def implied_prob(decimal_odds: float) -> float:
    """Probabilidad implicita CON vig (lo que cuelga la casa)."""
    return 1 / decimal_odds


def devig_two_way(p1: float, p2: float):
    """Quita el vig de un mercado de dos vias (metodo proporcional)."""
    total = p1 + p2
    return p1 / total, p2 / total


# ---- Descarga de lineas ---------------------------------------------------
def fetch_odds(sport: str) -> list:
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


# ---- Probabilidad justa por casa (devig dentro de cada casa) --------------
def _fair_by_book(game: dict) -> list:
    """Para cada casa con h2h de 2 vias, quita el vig DENTRO de esa misma casa.
    Devuelve [{equipo: prob_justa, '_book': nombre}, ...]."""
    out = []
    for book in game.get("bookmakers", []):
        dec = {}
        for market in book.get("markets", []):
            if market.get("key") != "h2h":
                continue
            for o in market["outcomes"]:
                dec[o["name"]] = american_to_decimal(o["price"])
        if len(dec) == 2:
            names = list(dec)
            fa, fb = devig_two_way(implied_prob(dec[names[0]]),
                                   implied_prob(dec[names[1]]))
            out.append({names[0]: fa, names[1]: fb,
                        "_book": book.get("title", "")})
    return out


def desacuerdo(game: dict) -> float:
    """Cuanto difieren las casas en la prob justa del equipo local."""
    ref = game.get("home_team")
    probs = [f[ref] for f in _fair_by_book(game) if ref in f]
    if len(probs) < 2:
        return 0.0
    return max(probs) - min(probs)


def _consenso_excluyendo(fairs: list, equipo: str, book_excluir: str):
    """Prob justa de consenso para `equipo`, EXCLUYENDO la casa `book_excluir`
    (la que da el mejor precio para ese equipo). Asi el fair y el precio nunca
    salen de la misma fuente.

    Ancla sharp si alguna de las casas restantes lo es; si no, mediana.
    Devuelve (prob, n_casas_usadas) o (None, 0) si no hay con quien comparar."""
    restantes = [f for f in fairs if f.get("_book") != book_excluir and equipo in f]
    if not restantes:
        return None, 0

    # ancla sharp entre las restantes
    for f in restantes:
        if any(s in f["_book"].lower() for s in SHARP_BOOKS):
            return f[equipo], len(restantes)

    vals = [f[equipo] for f in restantes]
    return statistics.median(vals), len(restantes)


def best_moneyline(game: dict):
    """Por equipo: mejor precio individual (line shopping) + prob justa de
    consenso de las OTRAS casas. Expone n_books = tamano minimo del consenso
    entre ambos equipos (para que value.py filtre por muestra)."""
    # --- mejor precio por equipo + de que casa salio ---
    teams = {}
    for book in game.get("bookmakers", []):
        for market in book.get("markets", []):
            if market.get("key") != "h2h":
                continue
            for outcome in market["outcomes"]:
                name = outcome["name"]
                dec = american_to_decimal(outcome["price"])
                if name not in teams or dec > teams[name]["decimal"]:
                    teams[name] = {"american": outcome["price"], "decimal": dec,
                                   "book": book["title"]}
    if len(teams) != 2:
        return None

    fairs = _fair_by_book(game)
    if not fairs:
        return None

    res = {}
    n_min = None
    for name, d in teams.items():
        fair, n = _consenso_excluyendo(fairs, name, d["book"])
        if fair is None:
            # No hay otra casa con quien comparar: cae a su propia implicita
            # (esto NO dara edge, es el caso degenerado de una sola casa).
            fair = implied_prob(d["decimal"])
            n = 0
        res[name] = {**d, "fair_prob": round(fair, 4)}
        n_min = n if n_min is None else min(n_min, n)

    res["matchup"] = f"{game.get('away_team')} @ {game.get('home_team')}"
    res["commence_time"] = game.get("commence_time")
    res["n_books"] = n_min or 0
    return res

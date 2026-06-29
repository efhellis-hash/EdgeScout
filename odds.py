"""
odds.py — Lineas del mercado + matematica honesta de probabilidad.

Aqui esta el baseline que NINGUN modelo debe ignorar: la probabilidad
implicita SIN vig del mercado. Es lo que el mercado realmente cree, y es
brutalmente dificil de superar. Tu "modelo" arranca desde aqui, no desde cero.

CAMBIO 2026-06 (correccion de pricing):
  El devig ahora se hace DENTRO de cada casa (sus dos patas) y luego se toma
  un consenso. ANTES se cruzaban casas (mejor cuota de A de DraftKings + mejor
  cuota de B de FanDuel) y se les quitaba el vig juntas: eso es invalido, la
  suma podia bajar de 1.0 y la "prob justa" salia deformada — y peor, se
  deformaba JUSTO en los juegos de mas desacuerdo, que son los que el sistema
  elige. Ahora:
    - fair_prob  = consenso de las casas (ancla sharp si hay Pinnacle/Circa,
                   si no la mediana por equipo, robusta a casas blandas).
    - american/decimal/book = MEJOR precio disponible por equipo (line shopping,
                   sin cambios) -> esto es lo que apuestas y lo que mide el CLV.
  El EV honesto = mejor precio disponible  vs  prob justa de consenso.
"""
import statistics
import requests
from config import THE_ODDS_API_KEY, SPORT_KEYS, MERCADOS, REGIONES

# Casas afiladas: si alguna esta presente, su linea (ya sin vig) es mejor ancla
# que el promedio de todo el mercado. Pinnacle vive en la region 'eu' de The Odds
# API; si tu REGIONES no la incluye, simplemente se cae al consenso por mediana.
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


# ---- Probabilidad justa (devig por casa, CORRECTO) ------------------------
def _fair_by_book(game: dict) -> list:
    """Para cada casa con mercado h2h de 2 vias, quita el vig DENTRO de esa misma
    casa y devuelve {equipo: prob_justa, '_book': nombre}. Esta es la unica forma
    valida de sacar prob justa: nunca se mezclan precios de casas distintas."""
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


def _consenso_fair(fairs: list):
    """Consenso de prob justa por equipo.
      1) Si hay casa sharp (Pinnacle/Circa/BetOnline), su devig es el ancla.
      2) Si no, mediana por equipo (resiste casas blandas que jalan la linea).
    Renormaliza para que sume 1.0. Devuelve dict con probs, fuente y n de casas."""
    if not fairs:
        return None
    equipos = [k for k in fairs[0] if k != "_book"]

    # 1) ancla sharp
    for f in fairs:
        if any(s in f["_book"].lower() for s in SHARP_BOOKS):
            anchor = {t: f[t] for t in equipos if t in f}
            if len(anchor) == 2:
                return {"probs": anchor, "fuente": f["_book"], "n": len(fairs)}

    # 2) mediana por equipo + renormalizacion
    med = {}
    for t in equipos:
        vals = [f[t] for f in fairs if t in f]
        if vals:
            med[t] = statistics.median(vals)
    s = sum(med.values())
    if s > 0:
        med = {t: v / s for t, v in med.items()}
    return {"probs": med, "fuente": f"consenso {len(fairs)} casas", "n": len(fairs)}


def desacuerdo(game: dict) -> float:
    """Cuanto difieren las casas en la prob justa (sin vig) del equipo local.
    Mayor desacuerdo = linea mas blanda en alguna casa = mejor candidato para
    gastar el analisis. Reusa el devig por-casa (la forma correcta)."""
    ref = game.get("home_team")
    probs = [f[ref] for f in _fair_by_book(game) if ref in f]
    if len(probs) < 2:
        return 0.0
    return max(probs) - min(probs)


def best_moneyline(game: dict):
    """Devuelve, por equipo:
      - american/decimal/book : el MEJOR precio disponible (line shopping). Es lo
        que apuestas y lo que compara el CLV. (sin cambios respecto a la version
        anterior).
      - fair_prob : prob justa de CONSENSO (devig por casa). ESTE es el arreglo:
        antes salia de cruzar mejores precios de casas distintas, lo cual deforma
        la probabilidad. Ahora es honesta.
    Tambien expone fair_source y n_books para que el resto del sistema pueda
    desconfiar de un consenso de pocas casas."""
    # --- mejor precio por equipo (no cambia) ---
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

    # --- prob justa de consenso (devig por casa) ---
    cons = _consenso_fair(_fair_by_book(game))
    if not cons:
        return None
    probs = cons["probs"]

    res = {}
    for name, d in teams.items():
        # si por alguna razon el consenso no cubre a un equipo, cae a su implicita
        fair = probs.get(name, implied_prob(d["decimal"]))
        res[name] = {**d, "fair_prob": round(fair, 4)}

    res["matchup"] = f"{game.get('away_team')} @ {game.get('home_team')}"
    res["commence_time"] = game.get("commence_time")
    res["fair_source"] = cons["fuente"]
    res["n_books"] = cons["n"]
    return res

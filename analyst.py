"""
analyst.py — Orquestador en MODO MERCADO (line shopping puro).

Cambio (2026-06): se ELIMINA el research con Haiku. EdgeScout ya no intenta
predecir mejor que el mercado; busca cuando la MEJOR cuota disponible paga por
encima del consenso sharp sin vig. Eso es edge real y medible (CLV), no opinion.

Flujo:
  1. fetch_odds trae las lineas de TODOS los juegos del dia.
  2. best_moneyline da, por equipo: prob justa de consenso + mejor cuota disponible.
  3. value.evaluate decide si la mejor cuota supera al consenso (EV > minimo).
  4. bankroll.py sugiere stake con tope.
  5. clv.py registra el pick para medir CLV (el juez honesto del edge).

Sin Haiku => sin llamadas a la API de modelo, sin throttle, sin elegir "los 3
juegos de mas desacuerdo". El line shopping es barato: se evaluan TODOS los juegos.

RECOMIENDA, NUNCA EJECUTA. Tu decides. La maquina no toca tu dinero.
"""
from odds import fetch_odds, best_moneyline
from value import evaluate
from bankroll import stake_usd, puede_operar
import clv


def analizar_juego(game: dict, sport: str, bankroll: float,
                   perdida_hoy: float = 0.0) -> dict:
    if not puede_operar(perdida_hoy, bankroll):
        return {"bloqueado": "Limite de perdida diaria alcanzado. No se opera hoy."}

    ml = best_moneyline(game)
    if not ml:
        return {"error": "Mercado moneyline incompleto"}

    matchup = ml["matchup"]
    commence_time = ml.get("commence_time")
    equipos = [k for k in ml if k not in ("matchup", "commence_time")]
    if len(equipos) != 2:
        return {"error": "Mercado incompleto"}

    n_books = ml.get("n_books", 0)
    recomendaciones = []
    for team in equipos:
        info = ml[team]
        fair = info["fair_prob"]                 # prob justa de consenso (otras casas)
        veredicto = evaluate(fair, info["decimal"], n_books)
        es_pick = veredicto["veredicto"] == "PASO"
        stake = (stake_usd(bankroll, fair, info["decimal"])
                 if es_pick else {"stake_pct": 0.0, "stake_usd": 0.0})

        # Guardamos SIEMPRE el analisis. En modo mercado no hay model_prob propio:
        # registramos la prob de consenso en ambas columnas (model_prob y market_fair)
        # para no romper la BD ni el dashboard, que esperan esas columnas.
        clv.log_analysis(
            sport, matchup, team, info["decimal"], fair, fair,
            veredicto["ev"], stake["stake_pct"], es_pick,
            factors=[f"Mejor precio: {info['book']} ({info['american']:+d})"],
            weather=None,
            reason=("Valor de line shopping: la mejor cuota disponible paga por "
                    "encima del consenso sharp sin vig." if es_pick else None),
            data_quality="mercado",
            commence_time=commence_time,
        )

        if not es_pick:
            continue

        recomendaciones.append({
            "pick": f"{team} ML @ {info['american']:+d} ({info['book']})",
            "prob_mercado_justa": f"{fair:.1%}",
            "ev": f"{veredicto['ev']:.1%}",
            "stake_sugerido": f"{stake['stake_pct']:.2%} de banca "
                              f"(${stake['stake_usd']})",
            "nota": "Recomendacion, no orden. Verifica la linea antes de actuar.",
        })

    return {"matchup": matchup,
            "recomendaciones": recomendaciones or
            ["Sin valor accionable. Lo correcto a veces es NO apostar."]}


def correr_dia(sport: str, bankroll: float, perdida_hoy: float = 0.0):
    clv.init_db()
    juegos = fetch_odds(sport)
    # Sin Haiku, el line shopping es barato: evaluamos TODOS los juegos del dia.
    salida = []
    for i, g in enumerate(juegos, 1):
        print(f"[EdgeScout] juego {i}/{len(juegos)}")
        salida.append(analizar_juego(g, sport, bankroll, perdida_hoy))
    print("[EdgeScout] corrida completa (modo mercado)")
    return salida


if __name__ == "__main__":
    import json
    BANKROLL = 1000.0
    resultados = correr_dia("MLB", BANKROLL)
    print(json.dumps(resultados, indent=2, ensure_ascii=False))
    print("\n--- CLV acumulado ---")
    print(json.dumps(clv.resumen_clv(), indent=2, ensure_ascii=False))

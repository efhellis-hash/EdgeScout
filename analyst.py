"""
analyst.py — Orquestador del AI Sports Analyst.

Flujo (en el orden CORRECTO, CLV-first):
  1. Trae lineas y calcula prob justa sin vig (ancla del mercado).
  2. El agente investiga (pitchers, lesiones, clima, forma) y ajusta la prob.
  3. value.py decide si hay valor real o es error de modelo (humildad de calibracion).
  4. bankroll.py sugiere stake con tope; respeta limite de perdida diaria.
  5. clv.py registra el pick para medir CLV (el unico juez honesto del edge).

RECOMIENDA, NUNCA EJECUTA. Tu decides. La maquina no toca tu dinero.
"""
from odds import fetch_odds, best_moneyline
from research import research_team_prob
from value import evaluate
from bankroll import stake_usd, puede_operar
import time
import clv

THROTTLE_SEGUNDOS = 8  # pausa entre juegos para no saturar el limite de tokens/min


def analizar_juego(game: dict, sport: str, bankroll: float,
                   perdida_hoy: float = 0.0, city: str = None) -> dict:
    if not puede_operar(perdida_hoy, bankroll):
        return {"bloqueado": "Limite de perdida diaria alcanzado. No se opera hoy."}

    ml = best_moneyline(game)
    if not ml:
        return {"error": "Mercado moneyline incompleto"}

    matchup = ml["matchup"]
    equipos = [k for k in ml if k not in ("matchup", "commence_time")]

    recomendaciones = []
    for team in equipos:
        info = ml[team]
        research = research_team_prob(matchup, sport, team,
                                      info["fair_prob"], city)
        if research.get("error") or research.get("parse_error"):
            continue

        model_prob = research.get("model_prob", info["fair_prob"])
        veredicto = evaluate(model_prob, info["fair_prob"], info["decimal"])
        es_pick = veredicto["veredicto"] == "PASO"
        stake = (stake_usd(bankroll, model_prob, info["decimal"])
                 if es_pick else {"stake_pct": 0.0, "stake_usd": 0.0})

        # Guardamos SIEMPRE el analisis (para poder desplegar cada juego)
        clv.log_analysis(
            sport, matchup, team, info["decimal"], model_prob,
            info["fair_prob"], veredicto["ev"], stake["stake_pct"], es_pick,
            factors=research.get("key_factors"),
            weather=research.get("weather_impact"),
            reason=research.get("adjustment_reason"),
            data_quality=research.get("data_quality"),
        )

        if not es_pick:
            continue  # se guardo el analisis, pero no es recomendacion

        recomendaciones.append({
            "pick": f"{team} ML @ {info['american']:+d} ({info['book']})",
            "prob_modelo": f"{model_prob:.1%}",
            "prob_mercado_justa": f"{info['fair_prob']:.1%}",
            "ev": f"{veredicto['ev']:.1%}",
            "stake_sugerido": f"{stake['stake_pct']:.2%} de banca "
                              f"(${stake['stake_usd']})",
            "calidad_datos": research.get("data_quality"),
            "razon": research.get("adjustment_reason"),
            "factores": research.get("key_factors"),
            "clima": research.get("weather_impact"),
            "falta_saber": research.get("missing_info"),
            "nota": "Recomendacion, no orden. Verifica la linea antes de actuar.",
        })

    return {"matchup": matchup,
            "recomendaciones": recomendaciones or
            ["Sin valor accionable. Lo correcto a veces es NO apostar."]}


def correr_dia(sport: str, bankroll: float, perdida_hoy: float = 0.0):
    clv.init_db()
    juegos = fetch_odds(sport)
    salida = []
    for g in juegos:
        salida.append(analizar_juego(g, sport, bankroll, perdida_hoy))
        time.sleep(THROTTLE_SEGUNDOS)
    return salida


if __name__ == "__main__":
    import json
    BANKROLL = 1000.0  # tu banca real en USD
    resultados = correr_dia("MLB", BANKROLL)
    print(json.dumps(resultados, indent=2, ensure_ascii=False))
    print("\n--- CLV acumulado ---")
    print(json.dumps(clv.resumen_clv(), indent=2, ensure_ascii=False))

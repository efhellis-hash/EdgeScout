"""
closing.py — Enciende el CLV. Captura la linea de cierre y calcula el valor real.

El problema que resuelve: log_analysis guardaba cada pick con clv_pct=NULL y
NADIE llamaba nunca a cerrar_pick(). Resultado: resumen_clv() devolvia siempre
n=0 y el "CLV acumulado" del dashboard estaba vacio para siempre. Medias tu edge
sin medir nada.

Que hace: cada pocos minutos (lo agenda app.py) revisa si algun pick esta por
empezar. Si lo esta, trae las odds una vez, toma el MEJOR precio disponible al
cierre para el equipo del pick (manzanas con manzanas: decimal_at_pick tambien
era el mejor precio) y llama a clv.cerrar_pick(). Empareja por
(matchup, commence_time, equipo).

Nota: el CLV NO necesita el resultado del juego (W/L). Calificar resultados es
otro paso, opcional, para ROI real; el CLV ya te dice si tienes edge sin el.
"""
import datetime as dt
from odds import fetch_odds, best_moneyline
import clv


def _norm_ct(iso):
    """commence_time -> UTC ISO canonico, para que matcheen ambos lados del join."""
    t = clv._parse_utc(iso)
    return t.isoformat() if t else iso


def cerrar_pendientes(sport: str, ventana_min_antes: int = 20):
    """Cierra los picks cuyo primer pitch esta dentro de la ventana.
    Devuelve un resumen liviano. Solo gasta API si hay algo que cerrar."""
    pend = clv.picks_por_cerrar(ventana_min_antes=ventana_min_antes,
                                ventana_min_despues=0)
    if not pend:
        return {"cerrados": 0, "en_ventana": 0}

    try:
        juegos = fetch_odds(sport)
    except Exception as e:
        print(f"[EdgeScout] cierre: no se pudieron traer odds: {e}")
        return {"cerrados": 0, "en_ventana": len(pend), "error": str(e)}

    # Index de la linea actual (= cierre) por (matchup, commence_time)
    idx = {}
    for g in juegos:
        ml = best_moneyline(g)
        if ml:
            idx[(ml["matchup"], _norm_ct(ml.get("commence_time")))] = ml

    cerrados = 0
    for p in pend:
        key = (p["matchup"], _norm_ct(p.get("commence_time")))
        ml = idx.get(key)
        if not ml or p["team"] not in ml:
            # La linea ya no esta (juego arrancado) o no matchea: se reintenta en
            # la proxima corrida mientras siga dentro de la ventana.
            continue
        closing_dec = ml[p["team"]]["decimal"]
        try:
            cpct = clv.cerrar_pick(p["id"], closing_dec)
            cerrados += 1
            print(f"[EdgeScout] cierre {p['team']} ({p['matchup']}): "
                  f"CLV {cpct:+.2f}%")
        except Exception as e:
            print(f"[EdgeScout] cierre fallo id={p['id']}: "
                  f"{type(e).__name__}: {e}")

    print(f"[EdgeScout] cierre: {cerrados}/{len(pend)} pick(s) en ventana")
    return {"cerrados": cerrados, "en_ventana": len(pend)}

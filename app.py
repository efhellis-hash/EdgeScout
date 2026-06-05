"""
app.py — Dashboard web de EdgeScout + scheduler, en un solo servicio.

- Flask sirve el panel: juegos de hoy con odds en vivo (sin vig), recomendaciones
  registradas y CLV acumulado.
- BackgroundScheduler corre el analisis diario sin bloquear la web.
- Boton "Analizar ahora" dispara una corrida bajo demanda en segundo plano.

Start command en Railway: python app.py
Reemplaza a scheduler.py (este ya incluye el scheduler).
"""
import os
import json
import time
import threading
import sqlite3
import datetime as dt
from zoneinfo import ZoneInfo

from flask import Flask, render_template_string, redirect, url_for
from apscheduler.schedulers.background import BackgroundScheduler

from config import DB_PATH
from odds import fetch_odds, best_moneyline
from analyst import correr_dia
import clv

SPORT = os.environ.get("SPORT", "MLB")
BANKROLL = float(os.environ.get("BANKROLL", "1000"))
RUN_HOUR = int(os.environ.get("RUN_HOUR_UTC", "20"))
CACHE_SEGUNDOS = 600  # cachea odds 10 min para no quemar el free tier de The Odds API
TZ = ZoneInfo("America/New_York")  # hora de Florida

app = Flask(__name__)
clv.init_db()

_cache = {"ts": 0, "data": None}
_running = {"flag": False}


# ----------------------------- Datos --------------------------------------
def _fmt_hora(iso: str) -> str:
    if not iso:
        return ""
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ)
        return t.strftime("%a %d, %I:%M %p ET")
    except Exception:
        return iso


def juegos_con_odds():
    now = time.time()
    if _cache["data"] is not None and now - _cache["ts"] < CACHE_SEGUNDOS:
        return _cache["data"]
    try:
        juegos = fetch_odds(SPORT)
    except Exception as e:
        return {"error": str(e)}
    filas = []
    for g in juegos:
        ml = best_moneyline(g)
        if not ml:
            continue
        equipos = [k for k in ml if k not in ("matchup", "commence_time")]
        lista = [{"team": t, "american": ml[t]["american"],
                  "fair": ml[t]["fair_prob"], "book": ml[t]["book"]}
                 for t in equipos]
        if lista:
            fav = max(lista, key=lambda e: e["fair"])
            for e in lista:
                e["favorito"] = (e is fav)
        filas.append({
            "matchup": ml["matchup"],
            "hora": _fmt_hora(ml.get("commence_time")),
            "equipos": lista,
        })
    _cache["data"] = filas
    _cache["ts"] = now
    return filas


def _dec_to_american(d):
    if d >= 2:
        return round((d - 1) * 100)
    return round(-100 / (d - 1))


def _parlay(legs):
    """Combina patas independientes. EV = prod(p)*prod(d) - 1.
    El edge se compone, pero la varianza explota y el error de modelo se multiplica."""
    if len(legs) < 2:
        return None
    D = P = 1.0
    for l in legs:
        D *= l["decimal"]
        P *= l["model_prob"]
    ev = P * (D - 1) - (1 - P)
    return {"legs": legs, "american": _dec_to_american(D),
            "prob": P, "ev": ev}


def armar_dashboard():
    juegos = juegos_con_odds()
    analisis = clv.analisis_recientes()
    by_match = {}
    for a in analisis:
        by_match.setdefault(a["matchup"], []).append(a)

    matchups_hoy = None
    if isinstance(juegos, list):
        matchups_hoy = set()
        for j in juegos:
            j["analisis"] = by_match.get(j["matchup"], [])
            matchups_hoy.add(j["matchup"])

    picks = [a for a in analisis if a["is_pick"]
             and (matchups_hoy is None or a["matchup"] in matchups_hoy)]
    picks.sort(key=lambda a: a["ev"], reverse=True)

    pick_dia = picks[0] if picks else None

    # Una sola pata por juego (las patas deben ser de juegos distintos)
    distintos, vistos = [], set()
    for a in picks:
        if a["matchup"] not in vistos:
            distintos.append(a)
            vistos.add(a["matchup"])
    dupleta = _parlay(distintos[:2]) if len(distintos) >= 2 else None
    tripleta = _parlay(distintos[:3]) if len(distintos) >= 3 else None

    return juegos, pick_dia, dupleta, tripleta


# ----------------------------- Corrida bajo demanda ------------------------
def _run_analysis():
    _running["flag"] = True
    try:
        correr_dia(SPORT, BANKROLL)
    except Exception as e:
        print(f"[EdgeScout] ERROR en analisis: {type(e).__name__}: {e}")
    finally:
        _running["flag"] = False


@app.route("/analizar")
def analizar():
    if not _running["flag"]:
        threading.Thread(target=_run_analysis, daemon=True).start()
    return redirect(url_for("home"))


# ----------------------------- Vista --------------------------------------
PAGINA = """
<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EdgeScout</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font-family:-apple-system,Segoe UI,Roboto,sans-serif;
         background:#0d1117; color:#e6edf3; padding:16px; }
  h1 { font-size:1.4rem; margin:0 0 4px; }
  h2 { font-size:1rem; color:#7d8590; margin:24px 0 10px;
       text-transform:uppercase; letter-spacing:.05em; }
  .sub { color:#7d8590; font-size:.8rem; margin-bottom:8px; }
  .card { background:#161b22; border:1px solid #30363d; border-radius:10px;
          padding:12px 14px; margin-bottom:8px; }
  .matchup { font-weight:600; margin-bottom:6px; }
  .hora { color:#7d8590; font-weight:400; font-size:.8rem; }
  .row { display:flex; justify-content:space-between; padding:3px 0;
         font-size:.9rem; }
  .odds { color:#58a6ff; font-variant-numeric:tabular-nums; }
  .fair { color:#7d8590; font-size:.8rem; }
  .fav { background:#1f6feb33; color:#79c0ff; font-size:.65rem; font-weight:600;
         padding:1px 6px; border-radius:6px; margin-left:6px;
         text-transform:uppercase; letter-spacing:.04em; }
  .pick-card { background:#161b22; border:1px solid #30363d; border-left:3px solid #3fb950;
               border-radius:10px; padding:12px 14px; margin-bottom:8px; }
  .pick-head { display:flex; justify-content:space-between; align-items:baseline;
               margin-bottom:6px; }
  .pick-team { font-weight:600; }
  .pill { font-size:.72rem; color:#7d8590; }
  .factor { font-size:.82rem; color:#c9d1d9; padding:3px 0 3px 14px;
            position:relative; }
  .factor:before { content:"▸"; position:absolute; left:0; color:#3fb950; }
  .meta { font-size:.78rem; color:#7d8590; margin-top:6px; }
  .btn { display:inline-block; background:#238636; color:#fff; padding:8px 14px;
         border-radius:8px; text-decoration:none; font-size:.9rem; }
  .clv { background:#161b22; border:1px solid #30363d; border-radius:10px;
         padding:12px 14px; }
  .pos { color:#3fb950; } .neg { color:#f85149; }
  table { width:100%; border-collapse:collapse; font-size:.82rem; }
  th,td { text-align:left; padding:6px 4px; border-bottom:1px solid #21262d; }
  th { color:#7d8590; font-weight:500; }
  .err { color:#f85149; }
  .empty { color:#7d8590; font-style:italic; padding:8px 0; }
  .star { background:#161b22; border:1px solid #3fb950; border-radius:10px;
          padding:14px; margin-bottom:8px; }
  .star .big { font-size:1.15rem; font-weight:700; color:#3fb950; }
  .parlay { background:#161b22; border:1px solid #30363d; border-radius:10px;
            padding:12px 14px; margin-bottom:8px; }
  .leg { font-size:.85rem; padding:2px 0; }
  .combo { font-size:.9rem; margin-top:8px; padding-top:8px;
           border-top:1px solid #21262d; }
  .warn { font-size:.76rem; color:#d29922; margin-top:6px; }
  details.game { background:#161b22; border:1px solid #30363d; border-radius:10px;
                 margin-bottom:8px; padding:0; }
  details.game summary { padding:12px 14px; cursor:pointer; list-style:none; }
  details.game summary::-webkit-details-marker { display:none; }
  details.game[open] summary { border-bottom:1px solid #21262d; }
  .gbody { padding:10px 14px 14px; }
  .team-an { margin:8px 0; padding-left:10px; border-left:2px solid #30363d; }
  .team-an.pickrow { border-left-color:#3fb950; }
</style></head><body>
  <h1>⚾ EdgeScout</h1>
  <div class="sub">{{ sport }} · banca ${{ bankroll }} ·
    {% if running %}<span class="pos">analizando…</span>
    {% else %}<a class="btn" href="/analizar">Analizar ahora</a>{% endif %}</div>

  <h2>Pick del día</h2>
  {% if pick_dia %}
    <div class="star">
      <div class="big">{{ pick_dia.team }}</div>
      <div class="meta">EV {{ (pick_dia.ev*100)|round(1) }}% ·
        modelo {{ (pick_dia.model_prob*100)|round(0)|int }}% vs
        mercado {{ (pick_dia.market_fair*100)|round(0)|int }}% ·
        stake {{ (pick_dia.stake_pct*100)|round(2) }}%</div>
      {% if pick_dia.reason %}<div class="meta">{{ pick_dia.reason }}</div>{% endif %}
      {% for f in pick_dia.factores %}<div class="factor">{{ f }}</div>{% endfor %}
    </div>
  {% else %}
    <div class="empty">Sin pick aun. Corre "Analizar ahora".</div>
  {% endif %}

  <h2>Dupleta / Tripleta</h2>
  {% if not dupleta %}
    <div class="empty">Hacen falta al menos 2 picks de juegos distintos.</div>
  {% else %}
    {% for nombre, par in [('Dupleta', dupleta), ('Tripleta', tripleta)] %}
      {% if par %}
      <div class="parlay">
        <div class="pick-team">{{ nombre }}</div>
        {% for l in par.legs %}<div class="leg">▸ {{ l.team }}
          <span class="fair">({{ (l.model_prob*100)|round(0)|int }}%)</span></div>{% endfor %}
        <div class="combo">
          Cuota combinada <span class="odds">{{ '%+d' % par.american }}</span> ·
          prob. de ganar <b class="{{ 'neg' if par.prob < 0.4 else '' }}">{{ (par.prob*100)|round(1) }}%</b> ·
          EV <span class="{{ 'pos' if par.ev>0 else 'neg' }}">{{ (par.ev*100)|round(1) }}%</span>
        </div>
        <div class="warn">⚠ La probabilidad de ganar cae mucho y los errores del
          modelo se multiplican. Mayor varianza que apostar los picks por separado.</div>
      </div>
      {% endif %}
    {% endfor %}
  {% endif %}

  <h2>Juegos de hoy <span class="sub">(toca para ver el análisis)</span></h2>
  {% if juegos.error %}
    <div class="card err">No se pudieron cargar las odds: {{ juegos.error }}</div>
  {% elif not juegos %}
    <div class="empty">Sin juegos cargados aun.</div>
  {% else %}
    {% for j in juegos %}
    <details class="game">
      <summary>
        <div class="matchup">{{ j.matchup }} <span class="hora">· {{ j.hora }}</span></div>
        {% for e in j.equipos %}
        <div class="row">
          <span>{{ e.team }}{% if e.favorito %}<span class="fav">favorito</span>{% endif %}
            <span class="fair">justa {{ (e.fair*100)|round(1) }}%</span></span>
          <span class="odds">{{ '%+d' % e.american }} <span class="fair">{{ e.book }}</span></span>
        </div>
        {% endfor %}
      </summary>
      <div class="gbody">
        {% if not j.analisis %}
          <div class="empty">Sin análisis aún. Corre "Analizar ahora".</div>
        {% else %}
          {% for a in j.analisis %}
          <div class="team-an {{ 'pickrow' if a.is_pick else '' }}">
            <div class="pick-team">{{ a.team }}
              {% if a.is_pick %}<span class="fav">pick · EV {{ (a.ev*100)|round(1) }}%</span>{% endif %}</div>
            <div class="meta">modelo {{ (a.model_prob*100)|round(0)|int }}% vs
              mercado {{ (a.market_fair*100)|round(0)|int }}% · datos {{ a.data_quality or 'n/d' }}</div>
            {% if a.reason %}<div class="meta">{{ a.reason }}</div>{% endif %}
            {% for f in a.factores %}<div class="factor">{{ f }}</div>{% endfor %}
            {% if a.weather and a.weather != 'N/A' %}<div class="meta">Clima: {{ a.weather }}</div>{% endif %}
          </div>
          {% endfor %}
        {% endif %}
      </div>
    </details>
    {% endfor %}
  {% endif %}

  <h2>CLV acumulado</h2>
  <div class="clv">
    {% if clv.n == 0 %}
      <span class="empty">{{ clv.lectura }}</span>
    {% else %}
      <div>Picks cerrados: <b>{{ clv.n }}</b></div>
      <div>CLV promedio:
        <b class="{{ 'pos' if clv.clv_promedio>0 else 'neg' }}">{{ clv.clv_promedio }}%</b></div>
      <div class="sub">{{ clv.lectura }}</div>
    {% endif %}
  </div>
</body></html>
"""


@app.route("/")
def home():
    juegos, pick_dia, dupleta, tripleta = armar_dashboard()
    return render_template_string(
        PAGINA, sport=SPORT, bankroll=int(BANKROLL), running=_running["flag"],
        juegos=juegos, pick_dia=pick_dia, dupleta=dupleta, tripleta=tripleta,
        clv=clv.resumen_clv(),
    )


# ----------------------------- Scheduler en background ---------------------
def _job():
    correr_dia(SPORT, BANKROLL)


sched = BackgroundScheduler(timezone="UTC")
sched.add_job(_job, "cron", hour=RUN_HOUR, minute=0)
sched.start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

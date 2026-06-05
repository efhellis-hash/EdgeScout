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
        filas.append({
            "matchup": ml["matchup"],
            "hora": _fmt_hora(ml.get("commence_time")),
            "equipos": [{"team": t, "american": ml[t]["american"],
                         "fair": ml[t]["fair_prob"], "book": ml[t]["book"]}
                        for t in equipos],
        })
    _cache["data"] = filas
    _cache["ts"] = now
    return filas


def picks_registrados(limit=50):
    try:
        con = sqlite3.connect(DB_PATH)
        rows = con.execute(
            "SELECT ts, matchup, team, decimal_at_pick, model_prob, ev, "
            "stake_pct, clv_pct, result FROM picks ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
        con.close()
        return rows
    except Exception:
        return []


# ----------------------------- Corrida bajo demanda ------------------------
def _run_analysis():
    _running["flag"] = True
    try:
        correr_dia(SPORT, BANKROLL)
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
</style></head><body>
  <h1>⚾ EdgeScout</h1>
  <div class="sub">{{ sport }} · banca ${{ bankroll }} ·
    {% if running %}<span class="pos">analizando…</span>
    {% else %}<a class="btn" href="/analizar">Analizar ahora</a>{% endif %}</div>

  <h2>Juegos de hoy</h2>
  {% if juegos.error %}
    <div class="card err">No se pudieron cargar las odds: {{ juegos.error }}</div>
  {% elif not juegos %}
    <div class="empty">Sin juegos cargados aun.</div>
  {% else %}
    {% for j in juegos %}
    <div class="card">
      <div class="matchup">{{ j.matchup }} <span class="hora">· {{ j.hora }}</span></div>
      {% for e in j.equipos %}
      <div class="row">
        <span>{{ e.team }} <span class="fair">justa {{ (e.fair*100)|round(1) }}%</span></span>
        <span class="odds">{{ '%+d' % e.american }} <span class="fair">{{ e.book }}</span></span>
      </div>
      {% endfor %}
    </div>
    {% endfor %}
  {% endif %}

  <h2>Recomendaciones registradas</h2>
  {% if not picks %}
    <div class="empty">Aun sin recomendaciones. Corre "Analizar ahora".</div>
  {% else %}
  <div class="card"><table>
    <tr><th>Fecha</th><th>Pick</th><th>Modelo</th><th>EV</th><th>CLV</th><th>Res</th></tr>
    {% for p in picks %}
    <tr>
      <td>{{ p[0][5:10] }}</td>
      <td>{{ p[2] }}</td>
      <td>{{ (p[4]*100)|round(0)|int }}%</td>
      <td>{{ (p[5]*100)|round(1) }}%</td>
      <td>{% if p[7] is not none %}<span class="{{ 'pos' if p[7]>0 else 'neg' }}">{{ p[7] }}%</span>{% else %}—{% endif %}</td>
      <td>{{ p[8] or '—' }}</td>
    </tr>
    {% endfor %}
  </table></div>
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
    return render_template_string(
        PAGINA, sport=SPORT, bankroll=int(BANKROLL), running=_running["flag"],
        juegos=juegos_con_odds(), picks=picks_registrados(),
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

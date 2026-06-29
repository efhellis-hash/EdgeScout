"""
app.py — Dashboard web de EdgeScout + scheduler, en un solo servicio.

- Flask sirve el panel: juegos de hoy con odds en vivo (sin vig), recomendaciones
  registradas y CLV acumulado.
- BackgroundScheduler corre el analisis diario sin bloquear la web.
- Boton "Analizar ahora" dispara una corrida bajo demanda en segundo plano.

Start command en Railway: python app.py
Reemplaza a scheduler.py (este ya incluye el scheduler).

CAMBIOS 2026-06:
  [1] El analisis se une al juego por (matchup, commence_time), no solo por
      matchup. Mata la duplicacion del mismo analisis sobre todos los juegos de
      una serie. Retrocompatible: filas viejas sin commence_time caen al join por
      matchup, pero solo si NO existe ningun dato fechado para esa serie.
  [2] Logos de equipos (CDN de ESPN, con fallback si la imagen no carga).
  [3] Cada juego/pick muestra CUANDO se analizo (timestamp en hora de Florida).
  [4] El scheduler respeta el flag _running (antes podia pisar una corrida manual).
  [5] Bandera "revisar" sobre EV absurdo (>20%): casi siempre es bug, no edge.
  [6] Endpoint /historial: ver picks y rechazados de cualquier fecha (hora ET).

CAMBIOS 2026-06 (UI + CLV encendido):
  [7] Interfaz nueva: estilo terminal de apuestas, mobile-first. Ticket del pick
      con pinstripes, barra modelo-vs-mercado, gauge de CLV, filtros, acordeones.
  [8] Boton "Analizar ahora" DINAMICO: dispara /analizar por fetch y consulta
      /estado en bucle; cuando termina, refresca solo. Sin recarga manual.
  [9] Cierre de CLV agendado: cada 5 min closing.cerrar_pendientes captura la
      linea de cierre de los picks por empezar y llena el CLV. Barato: solo pega
      a la API si hay picks en ventana.
"""
import os
import json
import time
import threading
import sqlite3
import datetime as dt
from zoneinfo import ZoneInfo

from flask import Flask, render_template_string, redirect, url_for, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

from config import DB_PATH
from odds import fetch_odds, best_moneyline
from analyst import correr_dia
import clv
import closing

SPORT = os.environ.get("SPORT", "MLB")
BANKROLL = float(os.environ.get("BANKROLL", "1000"))
RUN_HOUR = int(os.environ.get("RUN_HOUR_UTC", "20"))
CACHE_SEGUNDOS = 600  # cachea odds 10 min para no quemar el free tier de The Odds API
TZ = ZoneInfo("America/New_York")  # hora de Florida
EV_SOSPECHOSO = 0.20  # |EV| sobre esto = probable bug de datos, no oportunidad real

app = Flask(__name__)
clv.init_db()

_cache = {"ts": 0, "data": None}
_running = {"flag": False}


# ----------------------------- Logos de equipos ----------------------------
# CDN publico de ESPN. Si una abreviatura no resuelve, el <img onerror> oculta
# la imagen y no rompe nada. Ajusta una abreviatura puntual si algun logo no sale.
TEAM_ABBR = {
    "Arizona Diamondbacks": "ari", "Atlanta Braves": "atl",
    "Baltimore Orioles": "bal", "Boston Red Sox": "bos",
    "Chicago Cubs": "chc", "Chicago White Sox": "chw",
    "Cincinnati Reds": "cin", "Cleveland Guardians": "cle",
    "Colorado Rockies": "col", "Detroit Tigers": "det",
    "Houston Astros": "hou", "Kansas City Royals": "kc",
    "Los Angeles Angels": "laa", "Los Angeles Dodgers": "lad",
    "Miami Marlins": "mia", "Milwaukee Brewers": "mil",
    "Minnesota Twins": "min", "New York Mets": "nym",
    "New York Yankees": "nyy", "Athletics": "oak",
    "Oakland Athletics": "oak", "Philadelphia Phillies": "phi",
    "Pittsburgh Pirates": "pit", "San Diego Padres": "sd",
    "San Francisco Giants": "sf", "Seattle Mariners": "sea",
    "St. Louis Cardinals": "stl", "Tampa Bay Rays": "tb",
    "Texas Rangers": "tex", "Toronto Blue Jays": "tor",
    "Washington Nationals": "wsh",
}
_LOGO_BASE = "https://a.espncdn.com/i/teamlogos/mlb/500/{}.png"


def _logo(team):
    ab = TEAM_ABBR.get(team)
    return _LOGO_BASE.format(ab) if ab else ""


# ----------------------------- Datos --------------------------------------
def _fmt_hora(iso: str) -> str:
    """Formatea un ISO (de la API de odds, con Z) a hora de Florida."""
    if not iso:
        return ""
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ)
        return t.strftime("%a %d, %I:%M %p ET")
    except Exception:
        return iso


def _fmt_utc_ts(iso: str) -> str:
    """Formatea un timestamp UTC naive (el ts que guarda clv) a hora de Florida."""
    if not iso:
        return ""
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return t.astimezone(TZ).strftime("%a %d, %I:%M %p ET")
    except Exception:
        return iso


def _norm_ct(iso):
    """Canonicaliza commence_time a UTC ISO para que matcheen ambos lados del join
    aunque vengan con 'Z' o '+00:00'."""
    if not iso:
        return None
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return t.astimezone(dt.timezone.utc).isoformat()
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
        equipos = [k for k in ml if k not in ("matchup", "commence_time", "n_books")]
        lista = [{"team": t, "american": ml[t]["american"],
                  "fair": ml[t]["fair_prob"], "book": ml[t]["book"],
                  "logo": _logo(t)}
                 for t in equipos]
        if lista:
            fav = max(lista, key=lambda e: e["fair"])
            for e in lista:
                e["favorito"] = (e is fav)
        ct = ml.get("commence_time")
        filas.append({
            "matchup": ml["matchup"],
            "commence_time": ct,            # crudo, para el join
            "hora": _fmt_hora(ct),
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

    # Enriquecer cada fila de analisis (logo + cuando se analizo). pick_dia y las
    # patas del parlay apuntan a estos mismos dicts, asi que heredan el dato.
    for a in analisis:
        a["logo"] = _logo(a["team"])
        a["analizado"] = _fmt_utc_ts(a.get("ts"))

    # Indices para el join. Preferimos (matchup, commence_time); las filas viejas
    # sin commence_time van al indice por matchup (fallback retrocompatible).
    by_key = {}      # (matchup, commence_time_norm) -> [rows]
    by_match = {}    # matchup -> [rows]
    for a in analisis:
        by_match.setdefault(a["matchup"], []).append(a)
        ct = _norm_ct(a.get("commence_time"))
        if ct:
            by_key.setdefault((a["matchup"], ct), []).append(a)

    matchups_hoy = None
    if isinstance(juegos, list):
        matchups_hoy = set()
        for j in juegos:
            key = (j["matchup"], _norm_ct(j.get("commence_time")))
            if key in by_key:
                # Caso correcto: analisis fechado que corresponde a ESTE juego.
                j["analisis"] = by_key[key]
            else:
                rows = by_match.get(j["matchup"], [])
                # Solo usamos el fallback por matchup si TODAS las filas de esa
                # serie son viejas (sin fecha). Si ya hay datos fechados para la
                # serie pero ninguno casa con este juego, es que este juego no se
                # analizo: mejor mostrarlo vacio que embarrar el analisis de otro.
                if rows and all(not r.get("commence_time") for r in rows):
                    j["analisis"] = rows
                else:
                    j["analisis"] = []
            matchups_hoy.add(j["matchup"])
        # Los juegos analizados (con datos) primero
        juegos.sort(key=lambda j: len(j.get("analisis", [])), reverse=True)

    picks = [a for a in analisis if a["is_pick"]
             and (matchups_hoy is None or a["matchup"] in matchups_hoy)]
    picks.sort(key=lambda a: a["ev"], reverse=True)

    pick_dia = picks[0] if picks else None

    # Una sola pata por juego (las patas deben ser de juegos distintos)
    distintos, vistos = [], set()
    for a in picks:
        clave = (a["matchup"], _norm_ct(a.get("commence_time")))
        if clave not in vistos:
            distintos.append(a)
            vistos.add(clave)
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
    # Dispara el analisis en background. Si la peticion viene del fetch del boton
    # (?ajax=1) responde JSON; si alguien entra directo, redirige al home.
    if not _running["flag"]:
        threading.Thread(target=_run_analysis, daemon=True).start()
    if "ajax" in (request_args := _qs()):
        return jsonify({"running": _running["flag"]})
    return redirect(url_for("home"))


@app.route("/estado")
def estado():
    """Lo consulta el boton en bucle para saber cuando termino la corrida."""
    return jsonify({"running": _running["flag"]})


def _qs():
    """Querystring helper sin importar request global directamente arriba."""
    from flask import request
    return request.args


# ----------------------------- Vista --------------------------------------
PAGINA = """
<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EdgeScout</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@500;600;700&display=swap" rel="stylesheet">
<style>
  :root{
    color-scheme:dark;
    --bg:#070707; --bg-grad:#140d04; --panel:#121110; --panel-2:#1a1715;
    --line:#262320; --line-soft:#201d1a; --ink:#f3efe9; --muted:#9a9088;
    --muted-2:#6b635c;
    --amber:#ff9d2e; --amber-2:#ffb84d; --amber-dim:#3a2608; --amber-glow:#ff9d2e55;
    --fav:#3ddc84; --bad:#ff6b6b; --r:14px;
  }
  *{box-sizing:border-box} html,body{margin:0}
  body{font-family:Inter,-apple-system,Segoe UI,Roboto,sans-serif;
    background:radial-gradient(1300px 600px at 88% 8%,var(--bg-grad),transparent 55%),var(--bg);
    color:var(--ink);-webkit-font-smoothing:antialiased;line-height:1.45;padding-bottom:48px}
  .wrap{max-width:1180px;margin:0 auto;padding:0 20px}
  .mono{font-family:"JetBrains Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums}
  .pos{color:var(--amber)} .neg{color:var(--bad)}
  header{position:sticky;top:0;z-index:30;backdrop-filter:saturate(140%) blur(12px);
    background:linear-gradient(180deg,rgba(7,7,7,.92),rgba(7,7,7,.5));border-bottom:1px solid var(--line-soft)}
  .bar{max-width:1180px;margin:0 auto;padding:14px 20px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
  .logo{font-family:"Space Grotesk";font-weight:700;font-size:1.3rem;letter-spacing:-.02em;display:flex;align-items:center;gap:9px}
  .logo .ball{width:23px;height:23px;border-radius:50%;background:radial-gradient(circle at 35% 30%,#fff,#dfe6ee 40%,#aebccb);
    position:relative;box-shadow:0 0 0 1px #ffffff22,0 4px 12px #0008}
  .logo .ball:before,.logo .ball:after{content:"";position:absolute;inset:0;border-radius:50%;border:1.5px solid #d9534f55}
  .logo .ball:before{clip-path:inset(0 60% 0 0)} .logo .ball:after{clip-path:inset(0 0 0 60%)}
  .hmeta{margin-left:auto;display:flex;align-items:center;gap:14px;font-size:.78rem;color:var(--muted)}
  .hmeta b{color:var(--ink)}
  .chip-sport{background:var(--panel-2);border:1px solid var(--line);padding:3px 10px;border-radius:999px;color:var(--ink);font-weight:600;font-size:.74rem}
  .btn{border:0;cursor:pointer;font-family:inherit;background:linear-gradient(180deg,var(--amber-2),var(--amber));
    color:#1a0f00;font-weight:700;font-size:.82rem;padding:9px 16px;border-radius:10px;
    box-shadow:0 2px 0 #a85e0f,0 8px 22px var(--amber-glow);transition:transform .12s,box-shadow .12s}
  .btn:active{transform:translateY(2px);box-shadow:0 0 0 #a85e0f}
  .btn[disabled]{opacity:.7;cursor:default}
  .btn .spin{display:inline-block;width:11px;height:11px;border:2px solid #1a0f0044;border-top-color:#1a0f00;border-radius:50%;
    margin-right:6px;vertical-align:-1px;animation:rot .7s linear infinite}
  @keyframes rot{to{transform:rotate(360deg)}}
  .eyebrow{font-family:"Space Grotesk";text-transform:uppercase;letter-spacing:.14em;font-size:.7rem;color:var(--muted);
    margin:28px 2px 12px;display:flex;align-items:center;gap:10px}
  .eyebrow .ln{height:1px;background:var(--line);flex:1}
  .eyebrow .count{font-family:"JetBrains Mono";color:var(--muted-2);letter-spacing:0}
  .eyebrow a{color:var(--muted);text-decoration:none;font-size:.68rem;letter-spacing:.06em}
  .topbar{display:grid;grid-template-columns:1.4fr 1fr;gap:12px}
  @media (max-width:720px){.topbar{grid-template-columns:1fr}}
  .clv{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:15px;display:flex;align-items:center;gap:14px}
  .clv.off{border-style:dashed}
  .gauge{width:60px;height:60px;border-radius:50%;flex:none;display:grid;place-items:center}
  .gauge .hole{width:46px;height:46px;border-radius:50%;background:var(--panel);display:grid;place-items:center}
  .gauge .num{font-family:"JetBrains Mono";font-weight:700;font-size:.82rem}
  .clv .big{font-family:"Space Grotesk";font-weight:600;font-size:.9rem}
  .clv .sub{font-size:.73rem;color:var(--muted);margin-top:3px}
  .clv .n{font-size:.7rem;color:var(--muted-2);margin-top:5px}
  .clv .badge{background:var(--amber-dim);color:var(--amber);border:1px solid #6b4708;font-size:.6rem;font-weight:700;
    padding:2px 7px;border-radius:6px;display:inline-block;margin-bottom:5px}
  .summary{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:15px;display:flex;gap:18px;align-items:center}
  .summary .blk{text-align:center;flex:1}
  .summary .n{font-family:"JetBrains Mono";font-weight:700;font-size:1.5rem}
  .summary .lab{font-size:.64rem;text-transform:uppercase;letter-spacing:.1em;color:var(--muted-2);margin-top:2px}
  .filters{display:flex;gap:8px;margin:0 0 14px;flex-wrap:wrap}
  .fchip{border:1px solid var(--line);background:var(--panel);color:var(--muted);font-size:.76rem;font-weight:600;
    padding:6px 13px;border-radius:999px;cursor:pointer;transition:.15s;font-family:inherit}
  .fchip[aria-pressed="true"]{background:var(--amber-dim);border-color:#6b4708;color:var(--amber)}
  .fchip .c{opacity:.6;margin-left:4px;font-family:"JetBrains Mono"}
  .grid{display:grid;grid-template-columns:repeat(4,1fr);gap:11px;align-items:start}
  @media (max-width:1080px){.grid{grid-template-columns:repeat(3,1fr)}}
  @media (max-width:820px){.grid{grid-template-columns:repeat(2,1fr)}}
  @media (max-width:520px){.grid{grid-template-columns:1fr}}
  .game{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;transition:.2s;position:relative}
  .game.pick{border-color:var(--amber);box-shadow:0 0 0 1px var(--amber),0 8px 26px -8px var(--amber-glow)}
  .game.pick:before{content:"";position:absolute;inset:0;border-radius:12px;pointer-events:none;
    background:radial-gradient(120px 60px at 50% 0,var(--amber-glow),transparent 70%);opacity:.5}
  .g-head{padding:10px 12px;position:relative}
  .g-time{font-size:.64rem;color:var(--muted-2);font-family:"JetBrains Mono";display:flex;align-items:center;gap:6px;margin-bottom:8px}
  .g-pick{margin-left:auto;font-size:.56rem;font-weight:700;color:#1a0f00;background:var(--amber);padding:1px 6px;border-radius:5px;letter-spacing:.03em}
  .row{display:flex;align-items:center;gap:7px;padding:3px 0}
  .row img{width:19px;height:19px;border-radius:5px;background:var(--panel-2);padding:2px;flex:none}
  .row .nm{font-weight:600;font-size:.82rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .row .fav{font-size:.52rem;font-weight:700;color:var(--fav);background:#0f2a1c;border:1px solid #1f5238;padding:1px 4px;border-radius:4px}
  .row .od{margin-left:auto;font-family:"JetBrains Mono";font-weight:700;font-size:.82rem;color:var(--ink)}
  .row .ev{font-family:"JetBrains Mono";font-weight:700;font-size:.72rem;min-width:48px;text-align:right}
  .row.win .nm{color:var(--amber)}
  .g-foot{font-size:.6rem;color:var(--muted-2);padding:7px 12px 9px;border-top:1px solid var(--line-soft);display:flex;align-items:center;gap:5px}
  .g-foot .bk{color:var(--muted)}
  .empty{color:var(--muted);font-style:italic;font-size:.86rem;background:var(--panel);border:1px dashed var(--line);
    border-radius:var(--r);padding:24px;text-align:center;grid-column:1/-1}
  .err{color:var(--bad);background:var(--panel);border:1px solid #4a1f1f;border-radius:var(--r);padding:16px;font-size:.86rem;grid-column:1/-1}
  .foot{text-align:center;color:var(--muted-2);font-size:.72rem;margin-top:36px}
  .foot a{color:var(--muted)}
  @media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>

<header>
  <div class="bar">
    <div class="logo"><span class="ball"></span>EdgeScout</div>
    <div class="hmeta">
      <span class="chip-sport">{{ sport }}</span>
      <span>Florida · <b id="clock">&mdash;</b> ET</span>
      <button class="btn" id="analyzeBtn" onclick="analizar()"{% if running %} disabled{% endif %}>
        {% if running %}<span class="spin"></span>Analizando&hellip;{% else %}Analizar ahora{% endif %}</button>
    </div>
  </div>
</header>

<div class="wrap">

  <div class="eyebrow">Estado de hoy <span class="ln"></span>
    <a href="/historial">historial &#9656;</a></div>
  <div class="topbar">
    {% if clv.n == 0 %}
      <div class="clv off">
        <div class="gauge" style="background:conic-gradient(var(--amber) 0%,#1e1a16 0)"><div class="hole"><div class="num" style="color:var(--amber)">&mdash;</div></div></div>
        <div>
          <span class="badge">sin datos de cierre</span>
          <div class="big">A&uacute;n no se mide tu edge</div>
          <div class="sub">{{ clv.lectura }}</div>
        </div>
      </div>
    {% else %}
      {% set p = ((clv.clv_promedio + 3) / 6 * 100) %}
      {% set p = 0 if p < 0 else (100 if p > 100 else p) %}
      {% set gcol = 'var(--amber)' if clv.clv_promedio > 0 else 'var(--bad)' %}
      <div class="clv">
        <div class="gauge" style="background:conic-gradient({{ gcol }} {{ p }}%,#1e1a16 0)"><div class="hole"><div class="num {{ 'pos' if clv.clv_promedio>0 else 'neg' }}">{{ '%+.1f' % clv.clv_promedio }}%</div></div></div>
        <div>
          <div class="big {{ 'pos' if clv.clv_promedio>0 else 'neg' }}">{{ 'CLV positivo &mdash; edge real' if clv.clv_promedio>0 else 'CLV negativo &mdash; sin edge a&uacute;n' }}</div>
          <div class="sub">{{ clv.lectura }}</div>
          <div class="n">{{ clv.n }} picks cerrados</div>
        </div>
      </div>
    {% endif %}
    <div class="summary">
      <div class="blk"><div class="n {{ 'pos' if n_picks>0 else '' }}">{{ n_picks }}</div><div class="lab">Picks hoy</div></div>
      <div class="blk"><div class="n">{{ juegos|length if juegos is iterable and juegos is not mapping else 0 }}</div><div class="lab">Juegos</div></div>
      <div class="blk"><div class="n {{ 'pos' if mejor_ev>0 else '' }}">{{ '%+.1f' % (mejor_ev*100) }}%</div><div class="lab">Mejor EV</div></div>
    </div>
  </div>

  <div class="eyebrow">Juegos de hoy <span class="ln"></span>
    <span class="count">{{ juegos|length if juegos is iterable and juegos is not mapping else 0 }} juegos</span></div>
  {% if juegos.error %}
    <div class="grid"><div class="err">No se pudieron cargar las odds: {{ juegos.error }}</div></div>
  {% elif not juegos %}
    <div class="grid"><div class="empty">Sin juegos cargados a&uacute;n. Corre "Analizar ahora".</div></div>
  {% else %}
    <div class="filters" id="filters">
      <button class="fchip" aria-pressed="true" data-f="all">Todos <span class="c">{{ juegos|length }}</span></button>
      <button class="fchip" aria-pressed="false" data-f="pick">Con pick <span class="c">{{ n_picks }}</span></button>
      <button class="fchip" aria-pressed="false" data-f="none">Sin pick <span class="c">{{ juegos|length - n_con_pick }}</span></button>
    </div>
    <div class="grid" id="grid">
      {% for j in juegos %}
        {% set picks_set = j.analisis|selectattr('is_pick')|map(attribute='team')|list if j.analisis else [] %}
        {% set tiene_pick = picks_set|length > 0 %}
        {% set a_by_team = {} %}
        {% if j.analisis %}{% for a in j.analisis %}{% set _ = a_by_team.update({a.team: a}) %}{% endfor %}{% endif %}
        <div class="game {{ 'pick' if tiene_pick else '' }}" data-kind="{{ 'pick' if tiene_pick else 'none' }}">
          <div class="g-head">
            <div class="g-time">{{ j.hora }}{% if tiene_pick %}<span class="g-pick">PICK</span>{% endif %}</div>
            {% for e in j.equipos %}
              {% set a = a_by_team.get(e.team) %}
              {% set es_pick = e.team in picks_set %}
              <div class="row {{ 'win' if es_pick else '' }}">
                <img src="{{ e.logo }}" onerror="this.style.visibility='hidden'">
                <span class="nm">{{ e.team }}</span>
                {% if e.favorito %}<span class="fav">fav</span>{% endif %}
                <span class="od">{{ '%+d' % e.american }}</span>
                {% if a %}<span class="ev {{ 'pos' if a.ev>0 else 'neg' }}">{{ '%+.1f' % (a.ev*100) }}%</span>{% else %}<span class="ev" style="color:var(--muted-2)">&mdash;</span>{% endif %}
              </div>
            {% endfor %}
          </div>
          {% if j.analisis %}
            {% set a0 = j.analisis[0] %}
            <div class="g-foot">
              {% for e in j.equipos %}{% if e.team in picks_set %}<span class="bk">{{ e.book }}</span> &middot; {% endif %}{% endfor %}
              {{ a0.data_quality or 'mercado' }}{% if a0.analizado %} &middot; {{ a0.analizado }}{% endif %}
            </div>
          {% endif %}
        </div>
      {% endfor %}
    </div>
  {% endif %}

  <div class="foot">EdgeScout &middot; recomienda, nunca ejecuta &middot; <a href="/historial">historial &#9656;</a></div>
</div>

<script>
function tick(){document.getElementById('clock').textContent=new Date().toLocaleTimeString('es-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',hour12:true});}
tick();setInterval(tick,1000);
var fl=document.getElementById('filters');
if(fl){fl.addEventListener('click',function(e){var b=e.target.closest('.fchip');if(!b)return;
  document.querySelectorAll('.fchip').forEach(function(c){c.setAttribute('aria-pressed','false');});b.setAttribute('aria-pressed','true');
  var f=b.dataset.f;document.querySelectorAll('.game').forEach(function(g){g.style.display=(f==='all'||g.dataset.kind===f)?'':'none';});});}
var scanning=false;
function setBtn(l){var b=document.getElementById('analyzeBtn');b.disabled=l;b.innerHTML=l?'<span class="spin"></span>Analizando…':'Analizar ahora';}
function pollEstado(){fetch('/estado').then(function(r){return r.json();}).then(function(d){
  if(d.running){setTimeout(pollEstado,2500);}else{scanning=false;location.reload();}}).catch(function(){scanning=false;setBtn(false);});}
function analizar(){if(scanning)return;scanning=true;setBtn(true);
  fetch('/analizar?ajax=1').then(function(r){return r.json();}).then(function(){pollEstado();}).catch(function(){scanning=false;setBtn(false);});}
window.addEventListener('load',function(){if(document.getElementById('analyzeBtn').disabled)pollEstado();});
</script>
</body></html>
"""


@app.route("/debug")
def debug():
    try:
        con = sqlite3.connect(DB_PATH)
        total = con.execute("SELECT COUNT(*) FROM picks").fetchone()[0]
        picks = con.execute("SELECT COUNT(*) FROM picks WHERE is_pick=1").fetchone()[0]
        ult = con.execute(
            "SELECT ts, commence_time, matchup, team, model_prob, ev, is_pick "
            "FROM picks ORDER BY id DESC LIMIT 6").fetchall()
        con.close()
        return {"db_path": DB_PATH, "total_analisis": total,
                "total_picks": picks, "ultimos": ult}
    except Exception as e:
        return {"db_path": DB_PATH, "error": f"{type(e).__name__}: {e}"}


# ----------------------------- Historial por fecha -------------------------
# Muestra picks calificados y rechazados de cualquier dia. Filtra por la fecha
# en hora de Florida (el ts se guarda en UTC), asi "ayer" no se parte a medianoche.
HISTORIAL = """
<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EdgeScout · Historial</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@600;700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@600&display=swap" rel="stylesheet">
<style>
  :root{color-scheme:dark;--bg:#0a0e14;--panel:#121824;--line:#1f2a3a;--ink:#e8eef6;
    --muted:#7b8a9e;--edge:#37e0a6;--bad:#ff6b6b;--warn:#f5b945}
  *{box-sizing:border-box} body{margin:0;font-family:Inter,-apple-system,Segoe UI,Roboto,sans-serif;
    background:var(--bg);color:var(--ink);padding:16px;line-height:1.45}
  .wrap{max-width:520px;margin:0 auto}
  h1{font-family:"Space Grotesk";font-size:1.3rem;margin:0 0 4px;display:flex;align-items:center;gap:8px}
  .ball{width:20px;height:20px;border-radius:50%;background:radial-gradient(circle at 35% 30%,#fff,#aebccb)}
  a{color:#5aa8ff;text-decoration:none}
  .eyebrow{font-family:"Space Grotesk";text-transform:uppercase;letter-spacing:.12em;font-size:.72rem;
    color:var(--muted);margin:22px 0 10px}
  .nav{margin:10px 0 18px;font-size:.88rem;color:var(--muted);display:flex;align-items:center;gap:6px;flex-wrap:wrap}
  .nav a{padding:5px 10px;background:var(--panel);border:1px solid var(--line);border-radius:8px}
  .row{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 13px;margin-bottom:7px;font-size:.88rem}
  .row.pick{border-left:3px solid var(--edge)}
  .pos{color:var(--edge)} .neg{color:var(--bad)}
  .mono{font-family:"JetBrains Mono";font-variant-numeric:tabular-nums}
  .pill{background:#1b3b30;color:var(--edge);border:1px solid #2f6f57;font-size:.6rem;font-weight:700;
    padding:1px 7px;border-radius:6px;margin-left:6px}
  .suspect{color:var(--warn);font-weight:600;font-size:.72rem;margin-left:6px}
  .empty{color:var(--muted);font-style:italic;padding:8px 0}
  .err{color:var(--bad)} .mu{color:var(--muted)}
</style></head><body>
<div class="wrap">
  <h1><span class="ball"></span>Historial · {{ fecha }}</h1>
  <div class="nav">
    <a href="/historial/{{ ayer }}">◀ {{ ayer }}</a>
    <a href="/">inicio</a>
    <a href="/historial/{{ manana }}">{{ manana }} ▶</a>
    <span style="margin-left:auto">{{ total }} análisis</span>
  </div>

  {% if error %}
    <div class="row err">Error: {{ error }}</div>
  {% else %}
    <div class="eyebrow">Picks calificados ({{ picks|length }})</div>
    {% if not picks %}<div class="empty">Ningún pick pasó el filtro ese día.</div>{% endif %}
    {% for r in picks %}
      <div class="row pick">
        <b>{{ r.team }}</b> <span class="mu">· {{ r.matchup }}</span><span class="pill">PICK</span><br>
        <span class="mono">modelo {{ (r.model_prob*100)|round(0)|int }}%</span> ·
        EV <span class="mono {{ 'pos' if r.ev>0 else 'neg' }}">{{ '%+.1f' % (r.ev*100) }}%</span>
        {% if r.ev|abs > ev_sosp %}<span class="suspect">⚠ revisar</span>{% endif %}
      </div>
    {% endfor %}

    <div class="eyebrow">Rechazados ({{ resto|length }})</div>
    {% if not resto %}<div class="empty">Sin análisis rechazados ese día.</div>{% endif %}
    {% for r in resto %}
      <div class="row">
        {{ r.team }} <span class="mu">· {{ r.matchup }}</span> —
        EV <span class="mono {{ 'pos' if r.ev>0 else 'neg' }}">{{ '%+.1f' % (r.ev*100) }}%</span>
      </div>
    {% endfor %}
  {% endif %}
</div>
</body></html>
"""


@app.route("/historial")
@app.route("/historial/<fecha>")
def historial(fecha=None):
    hoy_et = dt.datetime.now(TZ).date()
    try:
        objetivo = dt.date.fromisoformat(fecha) if fecha else hoy_et - dt.timedelta(days=1)
    except ValueError:
        objetivo = hoy_et - dt.timedelta(days=1)

    ayer = (objetivo - dt.timedelta(days=1)).isoformat()
    manana = (objetivo + dt.timedelta(days=1)).isoformat()

    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT ts, matchup, team, model_prob, ev, is_pick "
            "FROM picks ORDER BY id DESC LIMIT 1000").fetchall()
        con.close()
    except Exception as e:
        return render_template_string(
            HISTORIAL, fecha=objetivo.isoformat(), picks=[], resto=[], total=0,
            ev_sosp=EV_SOSPECHOSO, ayer=ayer, manana=manana,
            error=f"{type(e).__name__}: {e}")

    def ts_a_et(ts):
        if not ts:
            return None
        try:
            t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=dt.timezone.utc)
            return t.astimezone(TZ)
        except Exception:
            return None

    dia = [r for r in rows if (e := ts_a_et(r["ts"])) and e.date() == objetivo]
    picks = sorted([r for r in dia if r["is_pick"]], key=lambda r: r["ev"], reverse=True)
    resto = sorted([r for r in dia if not r["is_pick"]], key=lambda r: r["ev"], reverse=True)

    return render_template_string(
        HISTORIAL, fecha=objetivo.isoformat(), picks=picks, resto=resto,
        total=len(dia), ev_sosp=EV_SOSPECHOSO, ayer=ayer, manana=manana, error=None)


@app.route("/")
def home():
    juegos, pick_dia, dupleta, tripleta = armar_dashboard()

    # Metricas para la barra de resumen y los filtros del grid.
    n_picks = 0          # total de lados con pick (puede haber 1 por juego)
    n_con_pick = 0       # juegos que tienen al menos un pick
    mejor_ev = 0.0
    if isinstance(juegos, list):
        for j in juegos:
            picks_j = [a for a in (j.get("analisis") or []) if a.get("is_pick")]
            if picks_j:
                n_con_pick += 1
                n_picks += len(picks_j)
            for a in (j.get("analisis") or []):
                if a.get("ev") is not None and a["ev"] > mejor_ev:
                    mejor_ev = a["ev"]

    return render_template_string(
        PAGINA, sport=SPORT, running=_running["flag"],
        juegos=juegos, clv=clv.resumen_clv(), ev_sosp=EV_SOSPECHOSO,
        n_picks=n_picks, n_con_pick=n_con_pick, mejor_ev=mejor_ev,
    )


# ----------------------------- Scheduler en background ---------------------
def _job():
    # Respeta el flag: si hay una corrida manual en curso, no la pisa.
    if _running["flag"]:
        print("[EdgeScout] cron saltado: ya hay una corrida en curso")
        return
    _run_analysis()


def _job_cierre():
    # Cierre de CLV: cada 5 min revisa si algun pick esta por empezar y captura
    # su linea de cierre. Barato: solo pega a la API si hay picks en ventana.
    try:
        closing.cerrar_pendientes(SPORT)
    except Exception as e:
        print(f"[EdgeScout] cierre ERROR: {type(e).__name__}: {e}")


sched = BackgroundScheduler(timezone="UTC")
sched.add_job(_job, "cron", hour=RUN_HOUR, minute=0)
sched.add_job(_job_cierre, "interval", minutes=5)
sched.start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

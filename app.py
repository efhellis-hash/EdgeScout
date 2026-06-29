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
        equipos = [k for k in ml if k not in ("matchup", "commence_time")]
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
    --bg:#0a0e14; --bg-grad:#0d1320; --panel:#121824; --panel-2:#161e2c;
    --line:#1f2a3a; --line-soft:#192435; --ink:#e8eef6; --muted:#7b8a9e;
    --muted-2:#5d6b7e; --edge:#37e0a6; --edge-dim:#1b3b30; --hot:#5aa8ff;
    --warn:#f5b945; --bad:#ff6b6b; --pin:#22304a; --r:14px;
  }
  *{box-sizing:border-box}
  html,body{margin:0}
  body{font-family:Inter,-apple-system,Segoe UI,Roboto,sans-serif;
    background:radial-gradient(1200px 600px at 50% -10%,var(--bg-grad),transparent 60%),var(--bg);
    color:var(--ink);-webkit-font-smoothing:antialiased;line-height:1.45;padding-bottom:48px}
  .wrap{max-width:520px;margin:0 auto;padding:0 16px}
  .mono{font-family:"JetBrains Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums}
  .pos{color:var(--edge)} .neg{color:var(--bad)} .neu{color:var(--hot)}

  header{position:sticky;top:0;z-index:30;backdrop-filter:saturate(140%) blur(12px);
    background:linear-gradient(180deg,rgba(10,14,20,.92),rgba(10,14,20,.6));
    border-bottom:1px solid var(--line-soft)}
  .bar{max-width:520px;margin:0 auto;padding:14px 16px;display:flex;align-items:center;gap:10px}
  .logo{font-family:"Space Grotesk";font-weight:700;font-size:1.25rem;letter-spacing:-.02em;
    display:flex;align-items:center;gap:8px}
  .logo .ball{width:22px;height:22px;border-radius:50%;
    background:radial-gradient(circle at 35% 30%,#fff,#dfe6ee 40%,#aebccb);
    position:relative;box-shadow:0 0 0 1px #ffffff22,0 4px 12px #0008}
  .logo .ball:before,.logo .ball:after{content:"";position:absolute;inset:0;border-radius:50%;
    border:1.5px solid #d9534f55}
  .logo .ball:before{clip-path:inset(0 60% 0 0)}
  .logo .ball:after{clip-path:inset(0 0 0 60%)}
  .clock{margin-left:auto;font-size:.72rem;color:var(--muted)}
  .clock b{color:var(--ink);font-weight:600}
  .sport-row{max-width:520px;margin:0 auto;padding:0 16px 12px;display:flex;align-items:center;gap:8px;
    font-size:.8rem;color:var(--muted)}
  .chip-sport{background:var(--panel-2);border:1px solid var(--line);padding:3px 9px;border-radius:999px;
    color:var(--ink);font-weight:600;font-size:.74rem}
  .dot{width:6px;height:6px;border-radius:50%;background:var(--edge);box-shadow:0 0 8px var(--edge);
    animation:pulse 2s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
  .btn{margin-left:auto;border:0;cursor:pointer;font-family:inherit;
    background:linear-gradient(180deg,#2bb583,#1e9c70);color:#04120c;font-weight:700;font-size:.8rem;
    padding:8px 14px;border-radius:10px;box-shadow:0 2px 0 #0c5a3f,0 8px 20px #1e9c7033;
    transition:transform .12s,box-shadow .12s}
  .btn:active{transform:translateY(2px);box-shadow:0 0 0 #0c5a3f}
  .btn[disabled]{opacity:.7;cursor:default}
  .btn .spin{display:inline-block;width:11px;height:11px;border:2px solid #04120c44;border-top-color:#04120c;
    border-radius:50%;margin-right:6px;vertical-align:-1px;animation:rot .7s linear infinite}
  @keyframes rot{to{transform:rotate(360deg)}}

  .eyebrow{font-family:"Space Grotesk";text-transform:uppercase;letter-spacing:.14em;font-size:.7rem;
    color:var(--muted);margin:26px 2px 10px;display:flex;align-items:center;gap:8px}
  .eyebrow .ln{height:1px;background:var(--line);flex:1}
  .eyebrow a{color:var(--muted);text-decoration:none;font-size:.68rem;letter-spacing:.06em}

  .ticket{position:relative;border-radius:var(--r);overflow:hidden;
    background:repeating-linear-gradient(180deg,transparent 0 13px,var(--pin) 13px 14px),
      linear-gradient(165deg,#13243a,#0f1a2b);
    border:1px solid #284a6b;box-shadow:0 18px 40px -18px #000,inset 0 1px 0 #ffffff0a}
  .ticket:before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;
    background:linear-gradient(180deg,var(--edge),var(--hot))}
  .tk-pad{padding:18px 18px 16px 20px}
  .tk-top{display:flex;align-items:center;gap:11px;margin-bottom:14px}
  .tk-logo{width:42px;height:42px;border-radius:11px;background:#0c1726;border:1px solid #ffffff14;
    object-fit:contain;padding:5px}
  .tk-team{font-family:"Space Grotesk";font-weight:700;font-size:1.35rem;letter-spacing:-.02em;line-height:1.05}
  .tk-side{font-size:.74rem;color:var(--muted);margin-top:2px}
  .tk-evwrap{margin-left:auto;text-align:right}
  .tk-ev{font-family:"JetBrains Mono";font-weight:700;font-size:2rem;line-height:1;letter-spacing:-.02em}
  .tk-evlab{font-size:.66rem;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);margin-top:3px}
  .flag{display:inline-block;background:#3a2e09;color:var(--warn);border:1px solid #6b520f;font-size:.62rem;
    font-weight:700;padding:1px 7px;border-radius:6px;margin-left:6px;vertical-align:2px}
  .vs{margin:4px 0 12px}
  .vs-labels{display:flex;justify-content:space-between;font-size:.72rem;margin-bottom:5px}
  .vs-labels .l{color:var(--edge)} .vs-labels .r{color:var(--hot)}
  .vs-track{position:relative;height:8px;border-radius:5px;background:#0a1320;overflow:hidden;border:1px solid #ffffff0a}
  .vs-model{position:absolute;left:0;top:0;bottom:0;border-radius:5px 0 0 5px;
    background:linear-gradient(90deg,var(--edge),#2bb583);width:0;transition:width 1s cubic-bezier(.2,.8,.2,1)}
  .vs-mark{position:absolute;top:-3px;width:2px;height:14px;background:var(--hot);box-shadow:0 0 8px var(--hot);
    transition:left 1s cubic-bezier(.2,.8,.2,1);left:0}
  .stake{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0 4px}
  .stat{background:#0c1726;border:1px solid var(--line);border-radius:10px;padding:8px 11px;flex:1;min-width:96px}
  .stat .k{font-size:.64rem;text-transform:uppercase;letter-spacing:.1em;color:var(--muted-2)}
  .stat .v{font-family:"JetBrains Mono";font-weight:700;font-size:1.05rem;margin-top:2px}
  .reason{font-size:.84rem;color:#c4d2e2;margin:12px 0 0;padding-top:12px;border-top:1px dashed #ffffff12}
  .factors{margin-top:9px;display:grid;gap:5px}
  .factor{font-size:.8rem;color:#aebccb;padding-left:18px;position:relative}
  .factor:before{content:"";position:absolute;left:2px;top:8px;width:6px;height:6px;border-radius:2px;
    background:var(--edge);transform:rotate(45deg)}
  .stamp{font-size:.68rem;color:var(--muted-2);margin-top:11px;display:flex;align-items:center;gap:6px}
  .empty{color:var(--muted);font-style:italic;font-size:.88rem;background:var(--panel);
    border:1px dashed var(--line);border-radius:var(--r);padding:18px;text-align:center}

  .clv{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:16px;
    display:flex;align-items:center;gap:16px}
  .gauge{width:78px;height:78px;border-radius:50%;flex:none;display:grid;place-items:center}
  .gauge .hole{width:60px;height:60px;border-radius:50%;background:var(--panel);display:grid;place-items:center}
  .gauge .num{font-family:"JetBrains Mono";font-weight:700;font-size:1rem}
  .clv-body .big{font-family:"Space Grotesk";font-weight:600;font-size:.95rem}
  .clv-body .sub{font-size:.78rem;color:var(--muted);margin-top:3px}
  .clv-body .n{font-size:.74rem;color:var(--muted-2);margin-top:6px}
  .clv.off{border-style:dashed}
  .clv.off .badge{background:#3a2e09;color:var(--warn);border:1px solid #6b520f;font-size:.64rem;font-weight:700;
    padding:2px 8px;border-radius:6px;display:inline-block;margin-bottom:6px}

  .parlay{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:14px 16px;margin-bottom:10px}
  .parlay h3{margin:0 0 10px;font-family:"Space Grotesk";font-size:.95rem;font-weight:700;
    display:flex;align-items:center;gap:8px}
  .parlay h3 .tag{font-size:.62rem;background:var(--panel-2);border:1px solid var(--line);color:var(--muted);
    padding:2px 7px;border-radius:6px;font-weight:600;letter-spacing:.04em;font-family:"JetBrains Mono"}
  .leg{display:flex;align-items:center;gap:9px;padding:6px 0;font-size:.88rem;border-bottom:1px solid var(--line-soft)}
  .leg:last-of-type{border-bottom:0}
  .leg img{width:22px;height:22px;border-radius:6px;background:#0c1726;padding:2px}
  .leg .p{margin-left:auto;font-family:"JetBrains Mono";font-size:.8rem;color:var(--muted)}
  .combo{display:flex;gap:8px;margin-top:11px;padding-top:11px;border-top:1px solid var(--line)}
  .combo .stat{flex:1}
  .warn-note{font-size:.74rem;color:var(--warn);margin-top:9px;display:flex;gap:7px;align-items:flex-start}
  .warn-note svg{flex:none;margin-top:1px}

  .filters{display:flex;gap:7px;margin:0 0 12px;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none}
  .filters::-webkit-scrollbar{display:none}
  .fchip{white-space:nowrap;border:1px solid var(--line);background:var(--panel);color:var(--muted);font-size:.76rem;
    font-weight:600;padding:6px 12px;border-radius:999px;cursor:pointer;transition:.15s;font-family:inherit}
  .fchip[aria-pressed="true"]{background:var(--edge-dim);border-color:#2f6f57;color:var(--edge)}
  .fchip .c{opacity:.6;margin-left:4px}

  .game{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);margin-bottom:9px;
    overflow:hidden;transition:border-color .2s}
  .game.has{border-color:#264a3c}
  .g-head{padding:13px 15px;cursor:pointer;display:block;user-select:none}
  .g-top{display:flex;align-items:center;gap:8px;margin-bottom:9px}
  .g-mu{font-weight:600;font-size:.92rem}
  .g-time{font-size:.72rem;color:var(--muted)}
  .g-pickdot{margin-left:auto;font-size:.62rem;font-weight:700;color:var(--edge);background:var(--edge-dim);
    border:1px solid #2f6f57;padding:2px 7px;border-radius:6px}
  .g-chev{margin-left:8px;color:var(--muted-2);transition:transform .25s;flex:none}
  .game.open .g-chev{transform:rotate(90deg)}
  .team-line{display:flex;align-items:center;gap:9px;padding:5px 0;font-size:.88rem}
  .team-line img{width:20px;height:20px;border-radius:6px;background:#0c1726;padding:2px;flex:none}
  .team-line .nm{font-weight:500}
  .badge-fav{font-size:.58rem;font-weight:700;color:var(--hot);background:#0e2440;border:1px solid #1c3e66;
    padding:1px 6px;border-radius:5px;text-transform:uppercase;letter-spacing:.04em}
  .team-line .fair{font-size:.72rem;color:var(--muted)}
  .team-line .od{margin-left:auto;font-family:"JetBrains Mono";font-weight:600;color:var(--hot);font-size:.86rem}
  .team-line .bk{font-size:.66rem;color:var(--muted-2);margin-left:5px}
  .g-body{max-height:0;overflow:hidden;transition:max-height .32s ease;border-top:1px solid transparent}
  .game.open .g-body{border-top-color:var(--line-soft)}
  .g-inner{padding:12px 15px 15px}
  .g-meta{font-size:.82rem;color:#c4d2e2;margin-bottom:8px}
  .an{margin:7px 0;padding:8px 10px;border-radius:9px;background:#0c1726;border-left:2px solid var(--line);
    font-size:.84rem;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .an.pick{border-left-color:var(--edge)}
  .an .tnm{font-weight:600}
  .an .cmp{color:var(--muted);font-size:.78rem}
  .an .evv{font-family:"JetBrains Mono";font-weight:700;margin-left:auto}
  .an .pk{font-size:.58rem;font-weight:700;color:var(--edge);background:var(--edge-dim);border:1px solid #2f6f57;
    padding:1px 6px;border-radius:5px}
  .suspect{color:var(--warn);font-weight:600;font-size:.72rem}
  .dq{font-size:.72rem;color:var(--muted-2);margin-top:6px}
  .err{color:var(--bad);background:var(--panel);border:1px solid #4a1f1f;border-radius:var(--r);padding:14px;font-size:.86rem}
  .foot{text-align:center;color:var(--muted-2);font-size:.72rem;margin-top:30px}
  .foot a{color:var(--muted)}
  @media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>

<header>
  <div class="bar">
    <div class="logo"><span class="ball"></span>EdgeScout</div>
    <div class="clock">Florida · <b id="clock">—</b> ET</div>
  </div>
  <div class="sport-row">
    <span class="dot"></span>
    <span class="chip-sport">{{ sport }}</span>
    <span>banca <b style="color:var(--ink)">${{ bankroll }}</b></span>
    <button class="btn" id="analyzeBtn" onclick="analizar()"
      {% if running %}disabled{% endif %}>
      {% if running %}<span class="spin"></span>Analizando…{% else %}Analizar ahora{% endif %}
    </button>
  </div>
</header>

<div class="wrap">

  <div class="eyebrow">Pick del día <span class="ln"></span>
    <a href="/historial">historial ▸</a></div>

  {% if pick_dia %}
    {% set susp = (pick_dia.ev|abs > ev_sosp) %}
    <div class="ticket">
      <div class="tk-pad">
        <div class="tk-top">
          <img class="tk-logo" src="{{ pick_dia.logo }}" onerror="this.style.visibility='hidden'">
          <div>
            <div class="tk-team">{{ pick_dia.team }}</div>
            <div class="tk-side">{{ pick_dia.matchup }}</div>
          </div>
          <div class="tk-evwrap">
            <div class="tk-ev {{ 'pos' if pick_dia.ev>0 else 'neg' }}">{{ '%+.1f' % (pick_dia.ev*100) }}%{% if susp %}<span class="flag">⚠</span>{% endif %}</div>
            <div class="tk-evlab">Valor esperado</div>
          </div>
        </div>
        <div class="vs">
          <div class="vs-labels">
            <span class="l">modelo <b class="mono">{{ (pick_dia.model_prob*100)|round(0)|int }}%</b></span>
            <span class="r">mercado <b class="mono">{{ (pick_dia.market_fair*100)|round(0)|int }}%</b></span>
          </div>
          <div class="vs-track">
            <div class="vs-model" data-w="{{ (pick_dia.model_prob*100)|round(0)|int }}"></div>
            <div class="vs-mark" data-l="{{ (pick_dia.market_fair*100)|round(0)|int }}"></div>
          </div>
        </div>
        <div class="stake">
          <div class="stat"><div class="k">Stake sugerido</div>
            <div class="v pos">{{ (pick_dia.stake_pct*100)|round(2) }}%</div></div>
          <div class="stat"><div class="k">Calidad datos</div>
            <div class="v" style="font-size:.9rem">{{ pick_dia.data_quality or 'n/d' }}</div></div>
        </div>
        {% if pick_dia.reason %}<div class="reason">{{ pick_dia.reason }}</div>{% endif %}
        {% if pick_dia.factores %}<div class="factors">
          {% for f in pick_dia.factores %}<div class="factor">{{ f }}</div>{% endfor %}
        </div>{% endif %}
        {% if pick_dia.analizado %}<div class="stamp">🕒 Analizado: {{ pick_dia.analizado }}</div>{% endif %}
      </div>
    </div>
  {% else %}
    <div class="empty">Sin pick aún. Corre "Analizar ahora".</div>
  {% endif %}

  <div class="eyebrow">CLV acumulado <span class="ln"></span></div>
  {% if clv.n == 0 %}
    <div class="clv off">
      <div class="gauge" style="background:conic-gradient(var(--warn) 0%, #18222f 0)">
        <div class="hole"><div class="num" style="color:var(--warn)">—</div></div></div>
      <div class="clv-body">
        <span class="badge">sin datos de cierre</span>
        <div class="big">Aún no se mide tu edge</div>
        <div class="sub">{{ clv.lectura }}</div>
      </div>
    </div>
  {% else %}
    {% set p = ((clv.clv_promedio + 3) / 6 * 100) %}
    {% set p = 0 if p < 0 else (100 if p > 100 else p) %}
    {% set gcol = 'var(--edge)' if clv.clv_promedio > 0 else 'var(--bad)' %}
    <div class="clv">
      <div class="gauge" style="background:conic-gradient({{ gcol }} {{ p }}%, #18222f 0)">
        <div class="hole"><div class="num {{ 'pos' if clv.clv_promedio>0 else 'neg' }}">{{ '%+.1f' % clv.clv_promedio }}%</div></div></div>
      <div class="clv-body">
        <div class="big {{ 'pos' if clv.clv_promedio>0 else 'neg' }}">
          {{ 'CLV positivo — señal de edge real' if clv.clv_promedio>0 else 'CLV negativo — sin edge aún' }}</div>
        <div class="sub">{{ clv.lectura }}</div>
        <div class="n">{{ clv.n }} picks cerrados</div>
      </div>
    </div>
  {% endif %}

  <div class="eyebrow">Dupleta / Tripleta <span class="ln"></span></div>
  {% if not dupleta %}
    <div class="empty">Hacen falta al menos 2 picks de juegos distintos.</div>
  {% else %}
    {% for nombre, par in [('Dupleta', dupleta), ('Tripleta', tripleta)] %}
      {% if par %}
      <div class="parlay">
        <h3>{{ nombre }} <span class="tag">{{ '%+d' % par.american }}</span></h3>
        {% for l in par.legs %}
        <div class="leg"><img src="{{ l.logo }}" onerror="this.style.visibility='hidden'">
          <span>{{ l.team }}</span><span class="p">{{ (l.model_prob*100)|round(0)|int }}%</span></div>
        {% endfor %}
        <div class="combo">
          <div class="stat"><div class="k">Prob. ganar</div>
            <div class="v {{ 'neg' if par.prob < 0.4 else '' }}">{{ (par.prob*100)|round(1) }}%</div></div>
          <div class="stat"><div class="k">EV combinado</div>
            <div class="v {{ 'pos' if par.ev>0 else 'neg' }}">{{ '%+.1f' % (par.ev*100) }}%</div></div>
        </div>
        <div class="warn-note">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M12 3 2 21h20L12 3z" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/><path d="M12 10v5M12 18h.01" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
          <span>La probabilidad de ganar cae fuerte y los errores del modelo se multiplican. Más varianza que apostar los picks por separado.</span>
        </div>
      </div>
      {% endif %}
    {% endfor %}
  {% endif %}

  <div class="eyebrow">Juegos de hoy <span class="ln"></span></div>
  {% if juegos.error %}
    <div class="err">No se pudieron cargar las odds: {{ juegos.error }}</div>
  {% elif not juegos %}
    <div class="empty">Sin juegos cargados aún.</div>
  {% else %}
    {% set con_pick = juegos|selectattr('analisis')|selectattr('analisis')|list %}
    <div class="filters" id="filters">
      <button class="fchip" aria-pressed="true" data-f="all">Todos <span class="c">{{ juegos|length }}</span></button>
      <button class="fchip" aria-pressed="false" data-f="value">Con pick</button>
      <button class="fchip" aria-pressed="false" data-f="none">Sin pick</button>
    </div>

    {% for j in juegos %}
      {% set tiene_pick = j.analisis and (j.analisis|selectattr('is_pick')|list|length > 0) %}
      <div class="game {{ 'has open' if j.analisis else '' }}" data-kind="{{ 'value' if tiene_pick else 'none' }}">
        <div class="g-head" onclick="toggle(this)">
          <div class="g-top">
            <span class="g-mu">{{ j.matchup }}</span>
            <span class="g-time">· {{ j.hora }}</span>
            {% if tiene_pick %}<span class="g-pickdot">PICK</span>{% endif %}
            <svg class="g-chev" width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="m9 6 6 6-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </div>
          {% for e in j.equipos %}
          <div class="team-line">
            <img src="{{ e.logo }}" onerror="this.style.visibility='hidden'">
            <span class="nm">{{ e.team }}</span>
            {% if e.favorito %}<span class="badge-fav">fav</span>{% endif %}
            <span class="fair">justa {{ (e.fair*100)|round(1) }}%</span>
            <span class="od">{{ '%+d' % e.american }}<span class="bk">{{ e.book }}</span></span>
          </div>
          {% endfor %}
        </div>
        <div class="g-body"><div class="g-inner">
          {% if not j.analisis %}
            <div class="empty" style="border:0;padding:6px 0">Sin análisis aún. Corre "Analizar ahora".</div>
          {% else %}
            {% set a0 = j.analisis[0] %}
            {% if a0.reason %}<div class="g-meta">{{ a0.reason }}</div>{% endif %}
            {% for f in a0.factores %}<div class="factor">{{ f }}</div>{% endfor %}
            {% if a0.weather and a0.weather != 'N/A' %}<div class="g-meta" style="margin-top:8px">Clima: {{ a0.weather }}</div>{% endif %}
            {% for a in j.analisis %}
            <div class="an {{ 'pick' if a.is_pick else '' }}">
              <span class="tnm">{{ a.team }}</span>
              <span class="cmp">modelo {{ (a.model_prob*100)|round(0)|int }}% vs mercado {{ (a.market_fair*100)|round(0)|int }}%</span>
              <span class="evv {{ 'pos' if a.ev>0 else 'neg' }}">{{ '%+.1f' % (a.ev*100) }}%</span>
              {% if a.ev|abs > ev_sosp %}<span class="suspect">⚠</span>{% endif %}
              {% if a.is_pick %}<span class="pk">PICK</span>{% endif %}
            </div>
            {% endfor %}
            <div class="dq">Calidad de datos: {{ a0.data_quality or 'n/d' }}{% if a0.analizado %} · {{ a0.analizado }}{% endif %}</div>
          {% endif %}
        </div></div>
      </div>
    {% endfor %}
  {% endif %}

  <div class="foot">EdgeScout · recomienda, nunca ejecuta · <a href="/historial">historial ▸</a></div>
</div>

<script>
// Reloj ET en vivo
function tick(){
  document.getElementById('clock').textContent =
    new Date().toLocaleTimeString('es-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',hour12:true});
}
tick(); setInterval(tick,1000);

// Barra modelo vs mercado
window.addEventListener('load',()=>{
  setTimeout(()=>{
    document.querySelectorAll('.vs-model').forEach(m=>m.style.width=m.dataset.w+'%');
    document.querySelectorAll('.vs-mark').forEach(m=>m.style.left=m.dataset.l+'%');
  },150);
  document.querySelectorAll('.game.open .g-body').forEach(b=>b.style.maxHeight=b.scrollHeight+'px');
  // Si el server ya estaba analizando al cargar, engancha el polling.
  if(document.getElementById('analyzeBtn').disabled) pollEstado();
});

// Acordeon
function toggle(head){
  const g=head.closest('.game'), body=g.querySelector('.g-body');
  const open=g.classList.toggle('open');
  body.style.maxHeight = open ? body.scrollHeight+'px' : '0px';
}

// Filtros
document.getElementById('filters')?.addEventListener('click',e=>{
  const b=e.target.closest('.fchip'); if(!b) return;
  document.querySelectorAll('.fchip').forEach(c=>c.setAttribute('aria-pressed','false'));
  b.setAttribute('aria-pressed','true');
  const f=b.dataset.f;
  document.querySelectorAll('.game').forEach(g=>{
    g.style.display = (f==='all'||g.dataset.kind===f) ? '' : 'none';
  });
});

// Analizar DINAMICO: dispara /analizar y consulta /estado hasta que termine.
let scanning=false;
function setBtn(loading){
  const b=document.getElementById('analyzeBtn');
  b.disabled=loading;
  b.innerHTML = loading ? '<span class="spin"></span>Analizando…' : 'Analizar ahora';
}
function pollEstado(){
  fetch('/estado').then(r=>r.json()).then(d=>{
    if(d.running){ setTimeout(pollEstado,2500); }
    else { scanning=false; location.reload(); }  // recarga UNA vez al terminar
  }).catch(()=>{ scanning=false; setBtn(false); });
}
function analizar(){
  if(scanning) return; scanning=true; setBtn(true);
  fetch('/analizar?ajax=1').then(r=>r.json()).then(()=>pollEstado())
    .catch(()=>{ scanning=false; setBtn(false); });
}
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
    return render_template_string(
        PAGINA, sport=SPORT, bankroll=int(BANKROLL), running=_running["flag"],
        juegos=juegos, pick_dia=pick_dia, dupleta=dupleta, tripleta=tripleta,
        clv=clv.resumen_clv(), ev_sosp=EV_SOSPECHOSO,
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

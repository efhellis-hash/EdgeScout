"""
clv.py — Closing Line Value. Tu unico examen honesto de si tienes edge.

No mide aciertos (eso engana). Mide si conseguiste mejor precio que la linea de
cierre. Si tu CLV es positivo de forma sostenida sobre una muestra grande,
tienes algo real. Si no, tu win rate es ilusion. Esto es lo PRIMERO que mides.

CAMBIO 2026-06: se anade la columna `commence_time` (hora de inicio del juego).
Es la clave que faltaba para distinguir juegos distintos de la MISMA serie.

CAMBIO 2026-06 (CLV encendido): se anade `picks_por_cerrar()`, que devuelve los
picks sin cierre cuya hora de inicio esta a punto de llegar. Es lo que faltaba
para que el CLV dejara de estar apagado: closing.py la usa para capturar la
linea de cierre y llamar a cerrar_pick(). Sin esto, clv_pct quedaba NULL para
siempre y resumen_clv() siempre devolvia n=0 (un CLV decorativo).
"""
import os
import json
import sqlite3
import datetime as dt
from config import DB_PATH


def init_db():
    carpeta = os.path.dirname(DB_PATH)
    if carpeta:
        os.makedirs(carpeta, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS picks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, sport TEXT, matchup TEXT, team TEXT,
            commence_time TEXT,     -- hora de inicio del juego (clave anti-duplicado)
            decimal_at_pick REAL, model_prob REAL, ev REAL, stake_pct REAL,
            closing_decimal REAL,   -- se llena tras el cierre
            clv_pct REAL,           -- se calcula al cerrar
            result TEXT,            -- W / L / Push tras el juego
            factors TEXT,           -- pitcher, lesiones, forma (JSON)
            weather TEXT,           -- impacto del clima
            reason TEXT,            -- por que se ajusto la prob
            data_quality TEXT,      -- alta/media/baja
            market_fair REAL,       -- prob justa del mercado (sin vig)
            is_pick INTEGER         -- 1 si paso el filtro de valor, 0 si no
        )
    """)
    migraciones = {"commence_time": "TEXT", "factors": "TEXT", "weather": "TEXT",
                   "reason": "TEXT", "data_quality": "TEXT", "market_fair": "REAL",
                   "is_pick": "INTEGER"}
    for col, tipo in migraciones.items():
        try:
            con.execute(f"ALTER TABLE picks ADD COLUMN {col} {tipo}")
        except sqlite3.OperationalError:
            pass
    con.commit()
    con.close()


def log_analysis(sport, matchup, team, decimal_at_pick, model_prob, market_fair,
                 ev, stake_pct, is_pick, factors=None, weather=None,
                 reason=None, data_quality=None, commence_time=None):
    """Guarda el analisis de UN equipo (haya pasado o no el filtro).
    is_pick=1 marca los que son recomendaciones reales."""
    con = sqlite3.connect(DB_PATH)
    cur = con.execute(
        "INSERT INTO picks (ts, sport, matchup, team, commence_time, "
        "decimal_at_pick, model_prob, market_fair, ev, stake_pct, is_pick, "
        "factors, weather, reason, data_quality) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (dt.datetime.utcnow().isoformat(), sport, matchup, team, commence_time,
         decimal_at_pick, model_prob, market_fair, ev, stake_pct,
         1 if is_pick else 0,
         json.dumps(factors, ensure_ascii=False) if factors else None,
         weather, reason, data_quality),
    )
    con.commit()
    pick_id = cur.lastrowid
    con.close()
    return pick_id


def analisis_recientes():
    """El análisis MÁS RECIENTE por (matchup, commence_time, equipo).

    Con varias corridas al día (cron 13-17 UTC), un mismo juego se analiza varias
    veces. El GROUP BY de SQL no basta porque commence_time puede variar de formato
    entre corridas (Z vs +00:00, segundos), y entonces deja pasar duplicados. Aquí
    traemos TODO ordenado por id desc y nos quedamos con el primero (más nuevo) de
    cada (matchup, commence_time_normalizado, equipo). Eso garantiza un único
    análisis por juego-equipo, sin duplicados en pantalla."""
    con = sqlite3.connect(DB_PATH)
    try:
        rows = con.execute(
            "SELECT ts, matchup, team, commence_time, decimal_at_pick, model_prob, "
            "market_fair, ev, stake_pct, is_pick, clv_pct, result, factors, weather, "
            "reason, data_quality FROM picks ORDER BY id DESC"
        ).fetchall()
    except Exception:
        con.close()
        return []
    con.close()

    def _norm(iso):
        t = _parse_utc(iso)
        return t.isoformat() if t else (iso or "")

    out = []
    vistos = set()
    for r in rows:
        clave = (r[1], _norm(r[3]), r[2])   # matchup, commence_time norm, team
        if clave in vistos:
            continue                         # ya tenemos el más reciente de este
        vistos.add(clave)
        try:
            factores = json.loads(r[12]) if r[12] else []
        except Exception:
            factores = []
        out.append({
            "ts": r[0], "matchup": r[1], "team": r[2], "commence_time": r[3],
            "decimal": r[4], "model_prob": r[5], "market_fair": r[6], "ev": r[7],
            "stake_pct": r[8], "is_pick": bool(r[9]), "clv_pct": r[10],
            "result": r[11], "factores": factores, "weather": r[13],
            "reason": r[14], "data_quality": r[15],
        })
    return out


# ---- Cierre de CLV --------------------------------------------------------
def _parse_utc(iso):
    """ISO -> datetime aware en UTC. None si no parsea."""
    if not iso:
        return None
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return t.astimezone(dt.timezone.utc)
    except Exception:
        return None


def picks_por_cerrar(ventana_min_antes: int = 20, ventana_min_despues: int = 0):
    """Picks reales (is_pick=1) aun sin cierre cuyo primer pitch esta cerca.

    Capturamos la linea de cierre en los minutos PREVIOS al inicio (la API de
    odds suele soltar la linea pre-juego apenas arranca). Por eso la ventana es
    sobre todo 'antes': por defecto desde 20 min antes hasta el inicio.

    Devuelve dicts con id, sport, matchup, team, commence_time, decimal_at_pick.
    Es una consulta barata (solo BD): closing.py solo gasta API si esto trae algo."""
    ahora = dt.datetime.now(dt.timezone.utc)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, sport, matchup, team, commence_time, decimal_at_pick "
        "FROM picks WHERE is_pick=1 AND clv_pct IS NULL AND closing_decimal IS NULL "
        "AND commence_time IS NOT NULL"
    ).fetchall()
    con.close()

    pend = []
    for r in rows:
        ct = _parse_utc(r["commence_time"])
        if not ct:
            continue
        delta_min = (ct - ahora).total_seconds() / 60.0
        if -ventana_min_despues <= delta_min <= ventana_min_antes:
            pend.append(dict(r))
    return pend


def cerrar_pick(pick_id: int, closing_decimal: float, result: str = None):
    """Tras el cierre: calcula CLV. CLV+ = tu cuota fue mejor que la de cierre."""
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT decimal_at_pick FROM picks WHERE id=?",
                      (pick_id,)).fetchone()
    if not row:
        con.close()
        raise ValueError("pick no encontrado")
    clv_pct = round((row[0] / closing_decimal - 1) * 100, 2)
    con.execute("UPDATE picks SET closing_decimal=?, clv_pct=?, result=? WHERE id=?",
                (closing_decimal, clv_pct, result, pick_id))
    con.commit()
    con.close()
    return clv_pct


def resumen_clv():
    """CLV promedio sobre los picks ya cerrados. La verdad sobre tu edge."""
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT clv_pct FROM picks WHERE clv_pct IS NOT NULL").fetchall()
    con.close()
    if not rows:
        return {"n": 0, "clv_promedio": None,
                "lectura": "Aun sin datos cerrados. Acumula muestra."}
    vals = [r[0] for r in rows]
    avg = sum(vals) / len(vals)
    lectura = ("CLV positivo: senal de edge real, sigue midiendo."
               if avg > 0 else
               "CLV negativo: no tienes edge. El win rate que veas es ruido.")
    return {"n": len(vals), "clv_promedio": round(avg, 2), "lectura": lectura}

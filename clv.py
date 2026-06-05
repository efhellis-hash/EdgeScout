"""
clv.py — Closing Line Value. Tu unico examen honesto de si tienes edge.

No mide aciertos (eso engana). Mide si conseguiste mejor precio que la linea de
cierre. Si tu CLV es positivo de forma sostenida sobre una muestra grande,
tienes algo real. Si no, tu win rate es ilusion. Esto es lo PRIMERO que mides.
"""
import sqlite3
import datetime as dt
from config import DB_PATH


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS picks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, sport TEXT, matchup TEXT, team TEXT,
            decimal_at_pick REAL, model_prob REAL, ev REAL, stake_pct REAL,
            closing_decimal REAL,   -- se llena tras el cierre
            clv_pct REAL,           -- se calcula al cerrar
            result TEXT             -- W / L / Push tras el juego
        )
    """)
    con.commit()
    con.close()


def log_pick(sport, matchup, team, decimal_at_pick, model_prob, ev, stake_pct):
    con = sqlite3.connect(DB_PATH)
    cur = con.execute(
        "INSERT INTO picks (ts, sport, matchup, team, decimal_at_pick, "
        "model_prob, ev, stake_pct) VALUES (?,?,?,?,?,?,?,?)",
        (dt.datetime.utcnow().isoformat(), sport, matchup, team,
         decimal_at_pick, model_prob, ev, stake_pct),
    )
    con.commit()
    pick_id = cur.lastrowid
    con.close()
    return pick_id


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

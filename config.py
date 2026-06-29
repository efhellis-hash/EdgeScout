"""
config.py — Configuracion central del AI Sports Analyst.
Todo parametro de riesgo vive aqui, no disperso en el codigo.
"""
import os

# ---- Claves de API (variables de entorno) --------------------------------
# ANTHROPIC_API_KEY ya no es obligatoria: en modo mercado no se usa Haiku.
# Se deja opcional para no romper si algun modulo viejo aun la importa.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
THE_ODDS_API_KEY = os.environ.get("THE_ODDS_API_KEY")        # the-odds-api.com
OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY")  # opcional (MLB/NFL)
MODEL = os.environ.get("MODEL", "claude-sonnet-4-6")

# ---- Deportes (empezar por UNO; MLB por defecto) -------------------------
SPORT_KEYS = {
    "MLB": "baseball_mlb",
    "NBA": "basketball_nba",
    "NFL": "americanfootball_nfl",
    "NHL": "icehockey_nhl",
}
OUTDOOR_SPORTS = {"MLB", "NFL"}

# ---- Parametros de RIESGO (regla dura, no negociable) --------------------
# MODO MERCADO: el edge de line shopping con casas de EE.UU. es delgado. 4% casi
# nunca dispara; 2% es el umbral realista, PERO solo si el consenso tiene muestra
# suficiente (MIN_CASAS) para no confundir ruido con valor.
EDGE_MINIMO = 0.02          # 2% de EV minimo (line shopping real es delgado)
MIN_CASAS = 4               # el consenso debe venir de >=4 casas, si no es ruido
KELLY_FRACCION = 0.25       # Kelly fraccionado (cuarto de Kelly)
STAKE_MAX_PCT = 0.02        # tope duro: 2% de banca por jugada
LIMITE_PERDIDA_DIARIA = 0.05  # 5% de banca; al tocarlo, deja de recomendar

# (DIVERGENCIA_MAX se elimino: en modo mercado no hay modelo propio que divergir)

# ---- Mercados a consultar ------------------------------------------------
MERCADOS = ["h2h", "spreads", "totals"]  # h2h = moneyline
REGIONES = "us"
DB_PATH = os.environ.get("DB_PATH", "analyst.db")

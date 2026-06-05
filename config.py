"""
config.py — Configuracion central del AI Sports Analyst.
Todo parametro de riesgo vive aqui, no disperso en el codigo.
"""
import os

# ---- Claves de API (variables de entorno) --------------------------------
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
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
EDGE_MINIMO = 0.04          # 4% de EV minimo para siquiera considerar
DIVERGENCIA_MAX = 0.10      # si |modelo - mercado| > 10% -> probable error, NO valor
KELLY_FRACCION = 0.25       # Kelly fraccionado (cuarto de Kelly)
STAKE_MAX_PCT = 0.02        # tope duro: 2% de banca por jugada
LIMITE_PERDIDA_DIARIA = 0.05  # 5% de banca; al tocarlo, el agente deja de recomendar

# ---- Mercados a consultar ------------------------------------------------
MERCADOS = ["h2h", "spreads", "totals"]  # h2h = moneyline
REGIONES = "us"
DB_PATH = os.environ.get("DB_PATH", "analyst.db")

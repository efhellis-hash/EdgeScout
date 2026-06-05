"""
research.py — El cerebro investigador.

Agente Claude que usa busqueda web (lesiones, alineaciones, pitcher abridor,
bullpen, forma reciente, matchups) + API de clima (viento/temp en MLB/NFL).
Devuelve una estimacion de probabilidad PROPIA con su razonamiento, partiendo
del numero del mercado como ancla.
"""
import os
import re
import json
import time
import requests
import anthropic
from anthropic import RateLimitError
from config import ANTHROPIC_API_KEY, MODEL, OPENWEATHER_API_KEY, OUTDOOR_SPORTS

# Modelo de investigacion: Haiku por defecto (mas rapido y mas barato que Sonnet,
# suficiente para buscar y resumir pitcher/lesiones/clima). Cambiable por env.
RESEARCH_MODEL = os.environ.get("RESEARCH_MODEL", "claude-haiku-4-5-20251001")

# max_retries deja que el SDK reintente solo ante throttling temporal
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, max_retries=4)


def _create_with_backoff(**kwargs):
    """Llama al modelo y, si pega el limite de tokens/minuto, espera y reintenta
    en vez de tumbar todo el job. Espera creciente: 10, 20, 40, 60s."""
    for intento in range(5):
        try:
            return client.messages.create(**kwargs)
        except RateLimitError:
            espera = min(60, 10 * (2 ** intento))
            print(f"[EdgeScout] limite de tokens/min, espero {espera}s...")
            time.sleep(espera)
    raise RuntimeError("Rate limit persistente tras varios reintentos")


def get_weather(city: str, country_code: str = "US") -> dict:
    if not OPENWEATHER_API_KEY:
        return {"error": "sin OPENWEATHER_API_KEY"}
    try:
        r = requests.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={"q": f"{city},{country_code}", "appid": OPENWEATHER_API_KEY,
                    "units": "imperial"},
            timeout=10,
        )
        r.raise_for_status()
        d = r.json()
        return {"temp_f": d["main"]["temp"], "wind_mph": d["wind"]["speed"],
                "wind_deg": d["wind"].get("deg"),
                "conditions": d["weather"][0]["description"]}
    except Exception as e:
        return {"error": str(e)}


TOOLS = [
    {
        "name": "get_weather",
        "description": "Clima del estadio (viento, temp). Solo MLB/NFL al aire libre.",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {"type": "string"},
                "country_code": {"type": "string"},
            },
            "required": ["city"],
        },
    },
    {"type": "web_search_20250305", "name": "web_search", "max_uses": 3},
]
CLIENT_TOOLS = {"get_weather": get_weather}

SYSTEM = """Eres un analista deportivo cuantitativo y escéptico.

ANCLA OBLIGATORIA: partes de la probabilidad justa del mercado (te la doy). El
mercado ya incorpora lesiones, clima y pitchers conocidos. Tu trabajo NO es
ignorarla, sino ajustarla SOLO si encuentras informacion que el mercado aun no
refleja (noticia muy reciente, dato mal valorado). Ajustes grandes son sospechosos:
casi siempre significan que TU estas equivocado, no el mercado.

INVESTIGA (con web_search, fuentes recientes):
- MLB: pitcher abridor confirmado de ambos lados, ERA/forma reciente, estado del
  bullpen (uso ayer), splits vs zurdos/derechos, lesiones, alineacion.
- Forma reciente del equipo, descanso, local/visitante, back-to-back.
- Clima si es al aire libre (get_weather): viento alto reduce carreras/HR.

SALIDA: responde SOLO con este JSON, sin texto extra ni backticks:
{
  "team": "equipo al que estimas probabilidad",
  "market_fair_prob": 0.00,        // la que te di
  "model_prob": 0.00,              // tu estimacion ajustada
  "adjustment_reason": "que dato nuevo justifica el ajuste, o 'sin ajuste'",
  "key_factors": ["factor 1 con fuente", "factor 2", ...],
  "weather_impact": "texto o 'N/A'",
  "data_quality": "alta | media | baja",
  "missing_info": "que no pudiste confirmar"
}"""


def research_team_prob(matchup: str, sport: str, team: str,
                       market_fair_prob: float, city: str = None) -> dict:
    user = (f"Deporte: {sport}\nPartido: {matchup}\n"
            f"Equipo a evaluar: {team}\n"
            f"Probabilidad justa del mercado (sin vig) para {team}: "
            f"{market_fair_prob:.4f}\n")
    if city and sport in OUTDOOR_SPORTS:
        user += f"Ciudad del estadio: {city}\n"
    user += "\nInvestiga y entrega el JSON."

    messages = [{"role": "user", "content": user}]
    for _ in range(8):
        resp = _create_with_backoff(
            model=RESEARCH_MODEL, max_tokens=1500, system=SYSTEM,
            tools=TOOLS, messages=messages,
        )
        tool_results = []
        for b in resp.content:
            if b.type == "tool_use" and b.name in CLIENT_TOOLS:
                out = CLIENT_TOOLS[b.name](**b.input)
                tool_results.append({"type": "tool_result",
                                     "tool_use_id": b.id,
                                     "content": json.dumps(out)})
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason == "tool_use":
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
            continue
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        parsed = _parse_json_robusto(text)
        return parsed if parsed is not None else {"raw": text, "parse_error": True}
    return {"error": "max turnos"}


def _parse_json_robusto(text: str):
    """Extrae JSON aunque venga con ```json fences o texto alrededor (Haiku
    a veces no devuelve JSON puro)."""
    if not text:
        return None
    t = text.strip()
    # quitar fences de markdown
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
        t = t.strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    # extraer el primer objeto {...} que aparezca
    m = re.search(r"\{.*\}", t, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None

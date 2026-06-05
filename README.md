# AI Sports Analyst

Agente de **análisis + gestión de riesgo** para deportes (no un bot apostador).
Recomienda, nunca ejecuta. Enfocado en MLB primero (pitchers, bullpen, clima).

## Qué hace

1. **odds.py** — Trae líneas (The Odds API) y calcula la probabilidad justa
   **sin vig** del mercado. Ese es tu baseline: lo que el mercado realmente cree.
2. **research.py** — Agente Claude con búsqueda web + clima. Investiga pitcher
   abridor, bullpen, lesiones, splits, forma reciente, viento. Ajusta la prob
   del mercado **solo** si encuentra algo que el mercado no refleja aún.
3. **value.py** — Detecta valor con **humildad de calibración**: si tu modelo
   se aleja demasiado del mercado, lo trata como error tuyo, no como oportunidad.
   Calcula EV real sobre la cuota con vig (lo que de verdad cobras).
4. **bankroll.py** — Kelly fraccionado (¼) con tope del 2%. Sin martingala.
   Límite de pérdida diaria: al tocarlo, deja de recomendar.
5. **clv.py** — Registra cada pick y mide **Closing Line Value**. Es el único
   juez honesto de si tienes edge. Mídelo PRIMERO, no al final.
6. **analyst.py** — Orquesta todo y arma la tarjeta de recomendación.

## Setup

```bash
pip install -r requirements.txt

export ANTHROPIC_API_KEY="..."
export THE_ODDS_API_KEY="..."      # the-odds-api.com (tiene tier gratuito limitado)
export OPENWEATHER_API_KEY="..."   # opcional, para viento en MLB/NFL

python analyst.py
```

Deploy igual que tus otros proyectos: Railway con las env vars cargadas.

## La disciplina (no la saltes)

- **Fase observación primero.** Corre el sistema sin dinero real. Registra cada
  pick y, tras el cierre de cada juego, llena `closing_decimal` con `clv.cerrar_pick()`.
- **Muestra grande antes de creer nada.** Una temporada parcial, no 10 juegos.
- **El veredicto lo da el CLV, no tu win rate.** `clv.resumen_clv()` te dice la
  verdad. CLV negativo sostenido = no tienes edge, punto.
- **Un deporte, un mercado.** Empieza con MLB moneyline o, mejor, props de
  pitcher (mercado menos eficiente). No abras los 4 deportes a la vez.

## Realidad económica

Suma la factura de datos ANTES de escalar. The Odds API tiene tier accesible;
los feeds en tiempo real de proveedores grandes son caros. Si el costo de datos
supera tu edge esperado, el proyecto no cierra — y eso solo lo sabes midiendo CLV.

# “””
BANCA NEURAL v5.0 — Motor de Predicción Multi-Mercado

- Todos los deportes con apuestas disponibles
- Mercados: 1X2, BTTS, Over/Under, Doble Oportunidad, Hándicap
- Combinadas automáticas (2-4 picks correlacionados)
- Filtro ultra-estricto: prob ≥ 65%, cuota ≥ 2.5, EV > 0
- Kelly fraccionado 25% por pick / 15% para combinadas
  “””

from flask import Flask, jsonify, request
from flask_cors import CORS
import requests, math, itertools
from datetime import datetime, timezone

app = Flask(**name**)
CORS(app)

# ── KEYS ────────────────────────────────────────────────────────────────────

RAPIDAPI_KEY  = “a001e8b536msh00b0fc34eb988dcp10faaajsn0514a8d603f2”
FORECAST_HOST = “game-forecast-api.p.rapidapi.com”
ODDS_API_KEY  = “e4eee6485c9bbb563f77293d7061aac1”
ODDS_BASE     = “https://api.the-odds-api.com/v4”

RAPIDAPI_HEADERS = {
“x-rapidapi-host”: FORECAST_HOST,
“x-rapidapi-key”:  RAPIDAPI_KEY,
“Content-Type”:    “application/json”,
}

# ── TODOS LOS DEPORTES / LIGAS ───────────────────────────────────────────────

ALL_SPORT_KEYS = [
# Basketball
“basketball_nba”, “basketball_ncaab”, “basketball_euroleague”,
“basketball_nbl”,
# Soccer
“soccer_epl”, “soccer_spain_la_liga”, “soccer_germany_bundesliga”,
“soccer_italy_serie_a”, “soccer_france_ligue_one”,
“soccer_uefa_champs_league”, “soccer_uefa_europa_league”,
“soccer_usa_mls”, “soccer_brazil_campeonato”,
“soccer_argentina_primera_division”, “soccer_netherlands_eredivisie”,
“soccer_portugal_primeira_liga”, “soccer_turkey_super_league”,
“soccer_mexico_ligamx”,
# Baseball
“baseball_mlb”, “baseball_npb”,
# American Football
“americanfootball_nfl”, “americanfootball_ncaaf”,
# Hockey
“icehockey_nhl”, “icehockey_sweden_allsvenskan”,
# Tennis
“tennis_atp_french_open”, “tennis_wta_french_open”,
“tennis_atp_us_open”, “tennis_wta_us_open”,
# MMA / Boxing
“mma_mixed_martial_arts”,
# Cricket
“cricket_icc_world_cup”, “cricket_psl”,
# Rugby
“rugbyleague_nrl”, “rugbyunion_six_nations”,
# Aussie Rules
“aussierules_afl”,
]

# Mercados disponibles por tipo de deporte

SOCCER_MARKETS   = [“h2h”, “btts”, “totals”, “double_chance”]
DEFAULT_MARKETS  = [“h2h”, “totals”]
TENNIS_MARKETS   = [“h2h”]

def get_markets_for_sport(sport_key: str) -> list:
if “soccer” in sport_key:
return SOCCER_MARKETS
if “tennis” in sport_key or “mma” in sport_key:
return TENNIS_MARKETS
return DEFAULT_MARKETS

# ── MATH CORE ────────────────────────────────────────────────────────────────

def implied_prob(odds: float) -> float:
return 1.0 / max(odds, 1.01)

def no_vig(probs: list) -> list:
t = sum(probs)
return [p / t for p in probs] if t > 0 else probs

def expected_value(true_prob: float, odds: float) -> float:
return round((true_prob * odds) - 1, 4)

def kelly(prob: float, odds: float, fraction: float = 0.25) -> float:
b = odds - 1
q = 1 - prob
k = (b * prob - q) / b
return round(max(0.0, min(k * fraction * 100, 5.0)), 2)

def combined_odds(odds_list: list) -> float:
result = 1.0
for o in odds_list:
result *= o
return round(result, 2)

def combined_prob(probs: list) -> float:
result = 1.0
for p in probs:
result *= p
return round(result, 4)

# ── GAME FORECAST API ────────────────────────────────────────────────────────

def fetch_forecast_events() -> list:
try:
r = requests.get(
f”https://{FORECAST_HOST}/events”,
headers=RAPIDAPI_HEADERS,
params={“status_code”: “NOT_STARTED”},
timeout=15,
)
r.raise_for_status()
data = r.json()
if isinstance(data, list): return data
if isinstance(data, dict):
for k in (“data”, “events”, “results”, “items”):
if k in data: return data[k]
return []
except Exception as e:
print(f”[ForecastAPI] {e}”)
return []

def extract_probs(raw: dict) -> dict:
“””
Parser corregido para la estructura real de Game Forecast API:
predictions[0].match_result.{home, away, draw}  (valores 0-100)
predictions[0].both_teams_score.{yes, no}
predictions[0].total_goals.{over_2_5, under_2_5}
“””
def to_f(v):
if v is None: return None
try:
f = float(v)
return f / 100 if f > 1 else f
except: return None

```
# Estructura real: predictions es una lista
preds = raw.get("predictions") or []
pred  = preds[0] if preds else {}

# match_result
mr   = pred.get("match_result") or {}
hp   = to_f(mr.get("home"))
ap   = to_f(mr.get("away"))
dp   = to_f(mr.get("draw"))

# both_teams_score
btts = pred.get("both_teams_score") or {}
btts_yes = to_f(btts.get("yes"))

# total_goals
tg      = pred.get("total_goals") or {}
over25  = to_f(tg.get("over_2_5"))
under25 = to_f(tg.get("under_2_5"))

# over/under 1.5
over15  = to_f(tg.get("over_1_5"))
under15 = to_f(tg.get("under_1_5"))

# home/away team goals
htg = pred.get("home_team_goals") or {}
atg = pred.get("away_team_goals") or {}

return {
    "home":      hp,
    "away":      ap,
    "draw":      dp,
    "btts_yes":  btts_yes,
    "over_25":   over25,
    "under_25":  under25,
    "over_15":   over15,
    "under_15":  under15,
    "home_over05": to_f(htg.get("over_0_5")),
    "away_over05": to_f(atg.get("over_0_5")),
}
```

def parse_event(raw: dict) -> dict | None:
# Estructura real: team_home.name / team_away.name
home = ((raw.get(“team_home”) or {}).get(“name”) or
raw.get(“home_team”) or raw.get(“homeTeam”) or “”)
away = ((raw.get(“team_away”) or {}).get(“name”) or
raw.get(“away_team”) or raw.get(“awayTeam”) or “”)
if not home or not away: return None

```
league = ((raw.get("league") or {}).get("name") or
          raw.get("league_name") or raw.get("sport") or "")
sport  = "soccer"  # Game Forecast API es principalmente soccer
start  = raw.get("start_at") or raw.get("start_time") or raw.get("commence_time") or ""

probs = extract_probs(raw)
if probs["home"] is None and probs["away"] is None: return None

# Normalizar home/away/draw
base = [probs["home"] or 0, probs["away"] or 0]
if probs["draw"] is not None: base.append(probs["draw"])
nv  = no_vig(base)
hp  = nv[0]
ap  = nv[1]
drp = nv[2] if len(nv) > 2 else None

return {
    "id":        raw.get("id") or "",
    "home":      home,
    "away":      away,
    "sport":     sport,
    "league":    league,
    "start":     start,
    "home_p":    round(hp, 4),
    "away_p":    round(ap, 4),
    "draw_p":    round(drp, 4) if drp is not None else None,
    "btts_p":    probs["btts_yes"],
    "over25_p":  probs["over_25"],
    "under25_p": probs["under_25"],
    "over15_p":  probs["over_15"],
    "under15_p": probs["under_15"],
}
```

# ── ODDS API ─────────────────────────────────────────────────────────────────

def fetch_odds_for_sport(sport_key: str) -> list:
markets = “,”.join(get_markets_for_sport(sport_key))
try:
r = requests.get(
f”{ODDS_BASE}/sports/{sport_key}/odds”,
params={
“apiKey”: ODDS_API_KEY,
“regions”: “eu,uk,us,au”,
“markets”: markets,
“oddsFormat”: “decimal”,
},
timeout=10,
)
r.raise_for_status()
return r.json()
except:
return []

def best_odds_for(bookmakers: list, market_key: str, outcome_name: str) -> float:
best = 1.01
for bk in bookmakers:
for mkt in bk.get(“markets”, []):
if mkt[“key”] == market_key:
for out in mkt.get(“outcomes”, []):
if outcome_name.lower() in out.get(“name”, “”).lower():
best = max(best, out[“price”])
return best

def match_event_to_odds(event: dict, odds_list: list) -> dict | None:
home = event[“home”].lower()
away = event[“away”].lower()
for g in odds_list:
gh = (g.get(“home_team”) or “”).lower()
ga = (g.get(“away_team”) or “”).lower()
hm = any(w in gh or gh in w for w in home.split() if len(w) > 3)
am = any(w in ga or ga in w for w in away.split() if len(w) > 3)
if hm or am: return g
return None

# ── MERCADOS ANALIZADOS ───────────────────────────────────────────────────────

def analyze_markets(event: dict, odds_game: dict | None) -> list:
“””
Para cada evento, analiza TODOS los mercados disponibles y devuelve
cada señal con prob, cuota, EV, tipo de apuesta (etiqueta humana).
“””
signals = []
bks = odds_game.get(“bookmakers”, []) if odds_game else []

```
def add(label: str, market: str, outcome: str,
        true_prob: float, odds: float, market_type: str):
    if true_prob is None or true_prob <= 0: return
    if odds <= 1.01: return
    ev = expected_value(true_prob, odds)
    signals.append({
        "label":       label,
        "market":      market,
        "outcome":     outcome,
        "market_type": market_type,
        "true_prob":   round(true_prob * 100, 1),
        "odds":        round(odds, 2),
        "ev_pct":      round(ev * 100, 1),
        "kelly_pct":   kelly(true_prob, odds),
        "_prob_raw":   true_prob,
        "_odds_raw":   odds,
    })

hp, ap = event["home_p"], event["away_p"]
dp     = event.get("draw_p")
home, away = event["home"], event["away"]

# ── 1X2 ──
ho = best_odds_for(bks, "h2h", home) if bks else round(1/hp*1.04,2)
ao = best_odds_for(bks, "h2h", away) if bks else round(1/ap*1.04,2)
add(f"{home} GANA",  "h2h", home, hp, ho, "1X2")
add(f"{away} GANA",  "h2h", away, ap, ao, "1X2")
if dp:
    do = best_odds_for(bks, "h2h", "Draw") if bks else round(1/dp*1.04,2)
    add("EMPATE", "h2h", "Draw", dp, do, "1X2")

# ── DOBLE OPORTUNIDAD (solo soccer) ──
if "soccer" in event.get("sport","").lower() and dp:
    p_1x = hp + dp
    p_x2 = ap + dp
    p_12 = hp + ap
    o_1x = best_odds_for(bks, "double_chance", "1X") if bks else round(1/p_1x*1.03,2)
    o_x2 = best_odds_for(bks, "double_chance", "X2") if bks else round(1/p_x2*1.03,2)
    o_12 = best_odds_for(bks, "double_chance", "12") if bks else round(1/p_12*1.03,2)
    add(f"{home} o Empate (1X)", "double_chance", "1X", p_1x, o_1x, "Doble Oportunidad")
    add(f"{away} o Empate (X2)", "double_chance", "X2", p_x2, o_x2, "Doble Oportunidad")
    add(f"{home} o {away} (12)", "double_chance", "12", p_12, o_12, "Doble Oportunidad")

# ── AMBOS MARCAN (BTTS) ──
bp = event.get("btts_p")
if bp:
    bo = best_odds_for(bks, "btts", "Yes") if bks else round(1/bp*1.04,2)
    add("AMBOS MARCAN — Sí", "btts", "Yes", bp, bo, "BTTS")
    bno = best_odds_for(bks, "btts", "No") if bks else round(1/(1-bp)*1.04,2)
    add("AMBOS MARCAN — No", "btts", "No", 1-bp, bno, "BTTS")

# ── MÁS/MENOS GOLES 2.5 ──
op = event.get("over25_p")
up = event.get("under25_p")
if op:
    oo = best_odds_for(bks, "totals", "Over") if bks else round(1/op*1.04,2)
    add("MÁS DE 2.5 GOLES",   "totals", "Over",  op, oo, "Over/Under")
if up:
    uo = best_odds_for(bks, "totals", "Under") if bks else round(1/up*1.04,2)
    add("MENOS DE 2.5 GOLES", "totals", "Under", up, uo, "Over/Under")

# ── MÁS/MENOS GOLES 1.5 ──
op15 = event.get("over15_p")
up15 = event.get("under15_p")
if op15:
    oo15 = round(1/op15*1.04,2)
    add("MÁS DE 1.5 GOLES",   "totals15", "Over",  op15, oo15, "Over/Under")
if up15:
    uo15 = round(1/up15*1.04,2)
    add("MENOS DE 1.5 GOLES", "totals15", "Under", up15, uo15, "Over/Under")

return signals
```

# ── PICK SELECTOR (ULTRA ESTRICTO) ───────────────────────────────────────────

MIN_PROB       = 0.65   # 65% mínimo
MIN_ODDS       = 2.50   # cuota mínima 2.5
MIN_EV         = 0.0    # EV positivo (cualquier valor > 0)

def select_best_signal(signals: list) -> dict | None:
“””
De todos los mercados del evento, elige la señal que cumple:
prob ≥ 65%, cuota ≥ 2.5, EV > 0.
Prioriza mayor EV.
“””
valid = [
s for s in signals
if s[”_prob_raw”] >= MIN_PROB
and s[”_odds_raw”] >= MIN_ODDS
and s[“ev_pct”] > MIN_EV * 100
]
if not valid: return None
return max(valid, key=lambda x: x[“ev_pct”])

# ── COMBINADA BUILDER ─────────────────────────────────────────────────────────

def build_combinadas(picks: list, min_legs: int = 2, max_legs: int = 4) -> list:
“””
Construye combinadas de 2 a 4 picks NO correlacionados (distintos partidos).
Filtra: cuota combinada ≥ 2.5, prob combinada ≥ 40% (realista).
Ordena por EV de la combinada.
“””
combinadas = []
# Agrupar por partido (1 pick por partido máximo)
partidos = {}
for p in picks:
key = p[“partido”]
if key not in partidos:
partidos[key] = p

```
pool = list(partidos.values())
if len(pool) < 2: return []

for r in range(min_legs, min(max_legs + 1, len(pool) + 1)):
    for combo in itertools.combinations(pool, r):
        odds_list = [c["odds"] for c in combo]
        prob_list = [c["_prob_raw"] for c in combo]

        c_odds = combined_odds(odds_list)
        c_prob = combined_prob(prob_list)

        if c_odds < MIN_ODDS: continue
        if c_prob < 0.35: continue   # mínimo 35% prob combinada

        c_ev    = round((c_prob * c_odds) - 1, 4)
        c_kelly = round(max(0, min(c_prob - (1 - c_prob) / (c_odds - 1), 0.05) * 0.15 * 100, 0), 2)

        if c_ev <= 0: continue

        combinadas.append({
            "picks":       [c["label"] + f" ({c['partido']})" for c in combo],
            "partidos":    [c["partido"] for c in combo],
            "deportes":    list({c["sport"] for c in combo}),
            "odds_list":   odds_list,
            "cuota_total": c_odds,
            "prob_total":  round(c_prob * 100, 1),
            "ev_pct":      round(c_ev * 100, 1),
            "kelly_pct":   c_kelly,
            "legs":        r,
        })

# Top 5 por EV descendente, sin repetir partidos entre combinadas top
combinadas.sort(key=lambda x: x["ev_pct"], reverse=True)
return combinadas[:8]
```

# ── EMOJI / FORMAT ────────────────────────────────────────────────────────────

def sport_emoji(sport: str) -> str:
s = (sport or “”).lower()
if “soccer” in s or (“football” in s and “american” not in s): return “⚽”
if “basketball” in s or “nba” in s: return “🏀”
if “baseball” in s: return “⚾”
if “hockey” in s: return “🏒”
if “tennis” in s: return “🎾”
if “american” in s or “nfl” in s: return “🏈”
if “mma” in s or “ufc” in s: return “🥊”
if “cricket” in s: return “🏏”
if “rugby” in s: return “🏉”
if “aussie” in s or “afl” in s: return “🦘”
return “🏟”

def format_date(iso: str) -> str:
if not iso: return “—”
try:
d = datetime.fromisoformat(iso.replace(“Z”, “+00:00”))
return d.strftime(”%d/%m %H:%M UTC”)
except: return iso

# ── MAIN ENDPOINT ─────────────────────────────────────────────────────────────

@app.route(”/api/signals”, methods=[“GET”])
def get_signals():
“””
Devuelve señales individuales + combinadas para todos los deportes.
Picks: prob ≥ 65%, cuota ≥ 2.5, EV > 0.
“””
# 1. Forecast API
raw_events = fetch_forecast_events()
events = []
for raw in raw_events:
ev_parsed = parse_event(raw)
if ev_parsed: events.append(ev_parsed)

```
# 2. Odds para todos los deportes (batch)
all_odds = {}  # sport_key → list of games
for sport_key in ALL_SPORT_KEYS:
    games = fetch_odds_for_sport(sport_key)
    if games:
        all_odds[sport_key] = games

# Flat list for matching
odds_flat = [g for games in all_odds.values() for g in games]

# Si no hay eventos de Forecast API, construir desde odds directamente
if not events:
    for sport_key, games in all_odds.items():
        for g in games:
            fake = {
                "id":       g.get("id",""),
                "home":     g.get("home_team",""),
                "away":     g.get("away_team",""),
                "sport":    sport_key,
                "league":   sport_key.replace("_"," ").title(),
                "start":    g.get("commence_time",""),
                "home_p":   0.50, "away_p": 0.50,
                "draw_p":   None, "btts_p": None,
                "over25_p": None, "under25_p": None,
            }
            events.append(fake)

# 3. Analizar cada evento
picks = []
for event in events:
    odds_game = match_event_to_odds(event, odds_flat)
    signals   = analyze_markets(event, odds_game)
    best      = select_best_signal(signals)
    if not best: continue

    bks = []
    if odds_game:
        bks = [b["title"] for b in odds_game.get("bookmakers", [])[:6]]

    # Attach metadata
    best.update({
        "partido":    f"{event['away']} vs {event['home']}",
        "home":       event["home"],
        "away":       event["away"],
        "sport":      event["sport"],
        "league":     event["league"],
        "start":      format_date(event["start"]),
        "emoji":      sport_emoji(event["sport"]),
        "bookmakers": bks,
        "_prob_raw":  best["_prob_raw"],
        "_odds_raw":  best["_odds_raw"],
    })
    picks.append(best)

# Ordenar por EV desc
picks.sort(key=lambda x: x["ev_pct"], reverse=True)

# Re-numerar
for i, p in enumerate(picks): p["numero"] = i + 1

# 4. Combinadas
combinadas = build_combinadas(picks) if len(picks) >= 2 else []

# Limpiar keys internas
for p in picks:
    p.pop("_prob_raw", None)
    p.pop("_odds_raw", None)

return jsonify({
    "success":          True,
    "timestamp":        datetime.now(timezone.utc).isoformat(),
    "total_events":     len(events),
    "total_picks":      len(picks),
    "total_combinadas": len(combinadas),
    "picks":            picks,
    "combinadas":       combinadas,
    "filters": {
        "min_prob_pct": MIN_PROB * 100,
        "min_odds":     MIN_ODDS,
        "min_ev_pct":   MIN_EV * 100,
    },
    "sources": [
        "Game Forecast API (RapidAPI)",
        "The Odds API — " + str(len(all_odds)) + " deportes",
    ],
})
```

@app.route(”/api/debug”, methods=[“GET”])
def debug():
raw = fetch_forecast_events()
return jsonify({“count”: len(raw), “sample”: raw[:2]})

@app.route(”/api/health”, methods=[“GET”])
def health():
return jsonify({“status”: “ok”, “ts”: datetime.now(timezone.utc).isoformat()})

if **name** == “**main**”:
app.run(debug=True, port=5000)

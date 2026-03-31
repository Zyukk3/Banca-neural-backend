from flask import Flask, jsonify, request
from flask_cors import CORS
import requests, math, itertools
from datetime import datetime, timezone

app = Flask(__name__)
CORS(app)

RAPIDAPI_KEY  = "a001e8b536msh00b0fc34eb988dcp10faaajsn0514a8d603f2"
FORECAST_HOST = "game-forecast-api.p.rapidapi.com"
ODDS_API_KEY  = "e4eee6485c9bbb563f77293d7061aac1"
ODDS_BASE     = "https://api.the-odds-api.com/v4"

RAPIDAPI_HEADERS = {
    "x-rapidapi-host": FORECAST_HOST,
    "x-rapidapi-key":  RAPIDAPI_KEY,
    "Content-Type":    "application/json",
}

ALL_SPORT_KEYS = [
    "basketball_nba", "basketball_ncaab", "basketball_euroleague",
    "baseball_mlb",
    "americanfootball_nfl", "americanfootball_ncaaf",
    "icehockey_nhl",
    "soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga",
    "soccer_italy_serie_a", "soccer_france_ligue_one",
    "soccer_uefa_champs_league", "soccer_uefa_europa_league",
    "soccer_usa_mls", "soccer_brazil_campeonato",
    "soccer_argentina_primera_division", "soccer_netherlands_eredivisie",
    "soccer_portugal_primeira_liga", "soccer_mexico_ligamx",
    "tennis_atp_french_open", "tennis_wta_french_open",
    "mma_mixed_martial_arts",
]

MIN_PROB = 0.55
MIN_ODDS = 1.50
MIN_EV   = 0.0


def to_f(v):
    if v is None:
        return None
    try:
        f = float(v)
        return f / 100.0 if f > 1.0 else f
    except Exception:
        return None


def no_vig(probs):
    t = sum(probs)
    if t <= 0:
        return probs
    return [p / t for p in probs]


def expected_value(true_prob, odds):
    return round((true_prob * odds) - 1.0, 4)


def kelly(prob, odds, fraction=0.25):
    b = odds - 1.0
    q = 1.0 - prob
    if b <= 0:
        return 0.0
    k = (b * prob - q) / b
    return round(max(0.0, min(k * fraction * 100.0, 5.0)), 2)


def combined_odds(odds_list):
    result = 1.0
    for o in odds_list:
        result *= o
    return round(result, 2)


def combined_prob(probs):
    result = 1.0
    for p in probs:
        result *= p
    return round(result, 4)


def sport_emoji(sport):
    s = (sport or "").lower()
    if "soccer" in s:
        return "soccer"
    if "basketball" in s or "nba" in s:
        return "basketball"
    if "baseball" in s:
        return "baseball"
    if "hockey" in s:
        return "hockey"
    if "tennis" in s:
        return "tennis"
    if "american" in s or "nfl" in s:
        return "football"
    if "mma" in s:
        return "mma"
    return "sport"


def format_date(iso):
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return d.strftime("%d/%m %H:%M UTC")
    except Exception:
        return str(iso)


def fetch_forecast_events():
    try:
        r = requests.get(
            "https://" + FORECAST_HOST + "/events",
            headers=RAPIDAPI_HEADERS,
            params={"status_code": "NOT_STARTED"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for k in ("data", "events", "results", "items"):
                if k in data:
                    return data[k]
        return []
    except Exception as e:
        print("ForecastAPI error: " + str(e))
        return []


def extract_probs(raw):
    preds = raw.get("predictions") or []
    pred  = preds[0] if preds else {}

    mr   = pred.get("match_result") or {}
    hp   = to_f(mr.get("home"))
    ap   = to_f(mr.get("away"))
    dp   = to_f(mr.get("draw"))

    btts    = pred.get("both_teams_score") or {}
    btts_yes = to_f(btts.get("yes"))

    tg      = pred.get("total_goals") or {}
    over25  = to_f(tg.get("over_2_5"))
    under25 = to_f(tg.get("under_2_5"))
    over15  = to_f(tg.get("over_1_5"))
    under15 = to_f(tg.get("under_1_5"))

    return {
        "home":      hp,
        "away":      ap,
        "draw":      dp,
        "btts_yes":  btts_yes,
        "over_25":   over25,
        "under_25":  under25,
        "over_15":   over15,
        "under_15":  under15,
    }


def parse_event(raw):
    th = raw.get("team_home") or {}
    ta = raw.get("team_away") or {}
    home = th.get("name") or raw.get("home_team") or ""
    away = ta.get("name") or raw.get("away_team") or ""
    if not home or not away:
        return None

    league = (raw.get("league") or {}).get("name") or raw.get("league_name") or ""
    sport  = "soccer"
    start  = raw.get("start_at") or raw.get("start_time") or raw.get("commence_time") or ""

    probs = extract_probs(raw)
    if probs["home"] is None and probs["away"] is None:
        return None

    base = [probs["home"] or 0.0, probs["away"] or 0.0]
    if probs["draw"] is not None:
        base.append(probs["draw"])
    nv  = no_vig(base)
    hp  = nv[0]
    ap  = nv[1]
    drp = nv[2] if len(nv) > 2 else None

    return {
        "id":        str(raw.get("id") or ""),
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


def fetch_odds_for_sport(sport_key):
    if "soccer" in sport_key:
        markets = "h2h,btts,totals,double_chance"
    elif "tennis" in sport_key or "mma" in sport_key:
        markets = "h2h"
    else:
        markets = "h2h,totals"
    try:
        r = requests.get(
            ODDS_BASE + "/sports/" + sport_key + "/odds",
            params={
                "apiKey":      ODDS_API_KEY,
                "regions":     "eu,uk,us,au",
                "markets":     markets,
                "oddsFormat":  "decimal",
            },
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


def best_odds_for(bookmakers, market_key, outcome_name):
    best = 1.01
    for bk in bookmakers:
        for mkt in bk.get("markets", []):
            if mkt["key"] == market_key:
                for out in mkt.get("outcomes", []):
                    if outcome_name.lower() in (out.get("name") or "").lower():
                        best = max(best, out["price"])
    return best


def match_event_to_odds(event, odds_list):
    home = event["home"].lower()
    away = event["away"].lower()
    for g in odds_list:
        gh = (g.get("home_team") or "").lower()
        ga = (g.get("away_team") or "").lower()
        hm = any(w in gh or gh in w for w in home.split() if len(w) > 3)
        am = any(w in ga or ga in w for w in away.split() if len(w) > 3)
        if hm or am:
            return g
    return None


def analyze_markets(event, odds_game):
    signals = []
    bks = odds_game.get("bookmakers", []) if odds_game else []

    def add(label, market, outcome, true_prob, odds, market_type):
        if true_prob is None or true_prob <= 0:
            return
        if odds <= 1.01:
            return
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

    hp   = event["home_p"]
    ap   = event["away_p"]
    dp   = event.get("draw_p")
    home = event["home"]
    away = event["away"]

    ho = best_odds_for(bks, "h2h", home) if bks else round(1.0 / max(hp, 0.01) * 1.04, 2)
    ao = best_odds_for(bks, "h2h", away) if bks else round(1.0 / max(ap, 0.01) * 1.04, 2)
    add(home + " GANA", "h2h", home, hp, ho, "1X2")
    add(away + " GANA", "h2h", away, ap, ao, "1X2")

    if dp is not None:
        do = best_odds_for(bks, "h2h", "Draw") if bks else round(1.0 / max(dp, 0.01) * 1.04, 2)
        add("EMPATE", "h2h", "Draw", dp, do, "1X2")

    if dp is not None:
        p_1x = hp + dp
        p_x2 = ap + dp
        p_12 = hp + ap
        o_1x = best_odds_for(bks, "double_chance", "1X") if bks else round(1.0 / max(p_1x, 0.01) * 1.03, 2)
        o_x2 = best_odds_for(bks, "double_chance", "X2") if bks else round(1.0 / max(p_x2, 0.01) * 1.03, 2)
        o_12 = best_odds_for(bks, "double_chance", "12") if bks else round(1.0 / max(p_12, 0.01) * 1.03, 2)
        add(home + " o Empate (1X)", "double_chance", "1X", p_1x, o_1x, "Doble Oportunidad")
        add(away + " o Empate (X2)", "double_chance", "X2", p_x2, o_x2, "Doble Oportunidad")
        add(home + " o " + away + " (12)", "double_chance", "12", p_12, o_12, "Doble Oportunidad")

    bp = event.get("btts_p")
    if bp is not None:
        bo  = best_odds_for(bks, "btts", "Yes") if bks else round(1.0 / max(bp, 0.01) * 1.04, 2)
        bno = best_odds_for(bks, "btts", "No")  if bks else round(1.0 / max(1.0 - bp, 0.01) * 1.04, 2)
        add("AMBOS MARCAN - Si",  "btts", "Yes", bp,        bo,  "BTTS")
        add("AMBOS MARCAN - No",  "btts", "No",  1.0 - bp,  bno, "BTTS")

    op = event.get("over25_p")
    up = event.get("under25_p")
    if op is not None:
        oo = best_odds_for(bks, "totals", "Over")  if bks else round(1.0 / max(op, 0.01) * 1.04, 2)
        add("MAS DE 2.5 GOLES",   "totals", "Over",  op, oo, "Over/Under")
    if up is not None:
        uo = best_odds_for(bks, "totals", "Under") if bks else round(1.0 / max(up, 0.01) * 1.04, 2)
        add("MENOS DE 2.5 GOLES", "totals", "Under", up, uo, "Over/Under")

    op15 = event.get("over15_p")
    up15 = event.get("under15_p")
    if op15 is not None:
        add("MAS DE 1.5 GOLES",   "totals15", "Over",  op15, round(1.0 / max(op15, 0.01) * 1.04, 2), "Over/Under")
    if up15 is not None:
        add("MENOS DE 1.5 GOLES", "totals15", "Under", up15, round(1.0 / max(up15, 0.01) * 1.04, 2), "Over/Under")

    return signals


def select_best_signal(signals):
    valid = [
        s for s in signals
        if s["_prob_raw"] >= MIN_PROB
        and s["_odds_raw"] >= MIN_ODDS
        and s["ev_pct"] > MIN_EV * 100
    ]
    if not valid:
        return None
    return max(valid, key=lambda x: x["ev_pct"])


def build_combinadas(picks):
    combinadas = []
    partidos = {}
    for p in picks:
        key = p["partido"]
        if key not in partidos:
            partidos[key] = p

    pool = list(partidos.values())
    if len(pool) < 2:
        return []

    for r in range(2, min(5, len(pool) + 1)):
        for combo in itertools.combinations(pool, r):
            odds_list = [c["odds"] for c in combo]
            prob_list = [c["_prob_raw"] for c in combo]
            c_odds = combined_odds(odds_list)
            c_prob = combined_prob(prob_list)
            if c_odds < MIN_ODDS:
                continue
            if c_prob < 0.30:
                continue
            c_ev = round((c_prob * c_odds) - 1.0, 4)
            if c_ev <= 0:
                continue
            c_kelly = round(max(0.0, min(c_prob - (1.0 - c_prob) / max(c_odds - 1.0, 0.01), 0.05) * 0.15 * 100, 0.0), 2)
            combinadas.append({
                "picks":       [c["label"] + " (" + c["partido"] + ")" for c in combo],
                "partidos":    [c["partido"] for c in combo],
                "deportes":    list({c["sport"] for c in combo}),
                "odds_list":   odds_list,
                "cuota_total": c_odds,
                "prob_total":  round(c_prob * 100, 1),
                "ev_pct":      round(c_ev * 100, 1),
                "kelly_pct":   c_kelly,
                "legs":        r,
            })

    combinadas.sort(key=lambda x: x["ev_pct"], reverse=True)
    return combinadas[:8]


@app.route("/api/signals", methods=["GET"])
def get_signals():
    raw_events = fetch_forecast_events()
    events = []
    for raw in raw_events:
        ev_parsed = parse_event(raw)
        if ev_parsed:
            events.append(ev_parsed)

    all_odds = {}
    for sport_key in ALL_SPORT_KEYS:
        games = fetch_odds_for_sport(sport_key)
        if games:
            all_odds[sport_key] = games

    odds_flat = [g for games in all_odds.values() for g in games]

    if not events and odds_flat:
        for sport_key, games in all_odds.items():
            for g in games:
                fake = {
                    "id":        g.get("id", ""),
                    "home":      g.get("home_team", ""),
                    "away":      g.get("away_team", ""),
                    "sport":     sport_key,
                    "league":    sport_key.replace("_", " ").title(),
                    "start":     g.get("commence_time", ""),
                    "home_p":    0.50,
                    "away_p":    0.50,
                    "draw_p":    None,
                    "btts_p":    None,
                    "over25_p":  None,
                    "under25_p": None,
                    "over15_p":  None,
                    "under15_p": None,
                }
                events.append(fake)

    picks = []
    for event in events:
        odds_game = match_event_to_odds(event, odds_flat)
        signals   = analyze_markets(event, odds_game)
        best      = select_best_signal(signals)
        if not best:
            continue
        bks = []
        if odds_game:
            bks = [b["title"] for b in odds_game.get("bookmakers", [])[:6]]
        best.update({
            "partido":    event["away"] + " vs " + event["home"],
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

    picks.sort(key=lambda x: x["ev_pct"], reverse=True)
    for i, p in enumerate(picks):
        p["numero"] = i + 1

    combinadas = build_combinadas(picks) if len(picks) >= 2 else []

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
        },
    })


@app.route("/api/debug", methods=["GET"])
def debug():
    raw = fetch_forecast_events()
    return jsonify({"count": len(raw), "sample": raw[:2]})


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "ts": datetime.now(timezone.utc).isoformat()})


if __name__ == "__main__":
    app.run(debug=True, port=5000)

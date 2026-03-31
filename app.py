from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import itertools
import math
from datetime import datetime, timezone, timedelta
from functools import reduce

app = Flask(__name__)
CORS(app)

#  KEYS 
RAPIDAPI_KEY       = "a001e8b536msh00b0fc34eb988dcp10faaajsn0514a8d603f2"
APIFOOTBALL_KEY    = "03d121598ae41fb3917b392ae9657647"
APIFOOTBALL_BASE   = "https://v3.football.api-sports.io"
APIFOOTBALL_HOST   = "v3.football.api-sports.io"
FORECAST_HOST      = "game-forecast-api.p.rapidapi.com"
FOOTBALL_PRED_HOST = "football-prediction-api.p.rapidapi.com"
ODDS_API_KEY       = "e4eee6485c9bbb563f77293d7061aac1"
ODDS_BASE          = "https://api.the-odds-api.com/v4"

AF_HEADERS = {
    "x-apisports-key": APIFOOTBALL_KEY,
    "x-rapidapi-host": APIFOOTBALL_HOST,
    "x-rapidapi-key":  APIFOOTBALL_KEY,
}

RAPIDAPI_HEADERS = {
    "x-rapidapi-host": RAPIDAPI_KEY,
    "x-rapidapi-key":  RAPIDAPI_KEY,
    "Content-Type":    "application/json",
}

#  CONFIG 
MIN_PROB    = 0.55
MIN_ODDS    = 1.50
MIN_EV      = 0.0
DATE_WINDOW = 7
SEASON      = 2025

# Top leagues with their API-Football IDs
TOP_LEAGUES = {
    39:  "Premier League",
    140: "La Liga",
    135: "Serie A",
    78:  "Bundesliga",
    61:  "Ligue 1",
    2:   "Champions League",
    3:   "Europa League",
    88:  "Eredivisie",
    94:  "Primeira Liga",
    203: "Super Lig",
    262: "Liga MX",
    253: "MLS",
    71:  "Brasileirao",
    128: "Liga Argentina",
}

ALL_SPORT_KEYS = [
    "basketball_nba", "baseball_mlb", "americanfootball_nfl",
    "icehockey_nhl", "soccer_epl", "soccer_spain_la_liga",
    "soccer_germany_bundesliga", "soccer_italy_serie_a",
    "soccer_france_ligue_one", "soccer_uefa_champs_league",
    "soccer_uefa_europa_league", "soccer_usa_mls",
    "soccer_brazil_campeonato", "soccer_argentina_primera_division",
    "soccer_netherlands_eredivisie", "soccer_portugal_primeira_liga",
    "soccer_mexico_ligamx", "mma_mixed_martial_arts",
]

#  MATH 

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

def calc_ev(true_prob, odds):
    return round((true_prob * odds) - 1.0, 4)

def calc_kelly(prob, odds, fraction=0.25):
    b = odds - 1.0
    q = 1.0 - prob
    if b <= 0:
        return 0.0
    k = (b * prob - q) / b
    return round(max(0.0, min(k * fraction * 100.0, 5.0)), 2)

def poisson_prob(lam, k):
    try:
        return (math.exp(-lam) * (lam ** k)) / math.factorial(k)
    except Exception:
        return 0.0

def poisson_over(lam_h, lam_a, line=2.5):
    total = 0.0
    for h in range(0, 9):
        for a in range(0, 9):
            if h + a > line:
                total += poisson_prob(lam_h, h) * poisson_prob(lam_a, a)
    return round(total, 4)

def poisson_btts(lam_h, lam_a):
    return round((1.0 - poisson_prob(lam_h, 0)) * (1.0 - poisson_prob(lam_a, 0)), 4)

def is_valid_date(date_str):
    if not date_str:
        return False
    try:
        d = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return -timedelta(hours=3) <= (d - now) <= timedelta(days=DATE_WINDOW)
    except Exception:
        return False

def format_date(iso):
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return d.strftime("%d/%m %H:%M UTC")
    except Exception:
        return str(iso)

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

def safe_get(url, headers, params=None, timeout=12):
    try:
        r = requests.get(url, headers=headers, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("Request error " + url + ": " + str(e))
        return {}

#  API-FOOTBALL: FIXTURES (partidos de hoy) 

def fetch_apifootball_fixtures():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data  = safe_get(
        APIFOOTBALL_BASE + "/fixtures",
        AF_HEADERS,
        {"date": today, "status": "NS"},
    )
    return data.get("response") or []

#  API-FOOTBALL: PREDICCIONES (6 algoritmos) 

def fetch_apifootball_prediction(fixture_id):
    data = safe_get(
        APIFOOTBALL_BASE + "/predictions",
        AF_HEADERS,
        {"fixture": fixture_id},
    )
    resp = data.get("response") or []
    return resp[0] if resp else {}

#  API-FOOTBALL: LESIONES 

def fetch_apifootball_injuries(fixture_id):
    data = safe_get(
        APIFOOTBALL_BASE + "/injuries",
        AF_HEADERS,
        {"fixture": fixture_id},
    )
    return data.get("response") or []

#  API-FOOTBALL: H2H 

def fetch_apifootball_h2h(team_home_id, team_away_id, last=10):
    data = safe_get(
        APIFOOTBALL_BASE + "/fixtures/headtohead",
        AF_HEADERS,
        {"h2h": str(team_home_id) + "-" + str(team_away_id), "last": last},
    )
    return data.get("response") or []

#  API-FOOTBALL: ESTADISTICAS DE EQUIPO 

def fetch_team_stats(team_id, league_id):
    data = safe_get(
        APIFOOTBALL_BASE + "/teams/statistics",
        AF_HEADERS,
        {"team": team_id, "league": league_id, "season": SEASON},
    )
    return data.get("response") or {}

#  API-FOOTBALL: STANDINGS (forma reciente) 

def fetch_standings(league_id):
    data = safe_get(
        APIFOOTBALL_BASE + "/standings",
        AF_HEADERS,
        {"league": league_id, "season": SEASON},
    )
    try:
        return data["response"][0]["league"]["standings"][0]
    except Exception:
        return []

#  PROFESSIONAL PROBABILITY ENGINE 

def form_string_to_score(form_str):
    if not form_str:
        return 0.5
    recent = form_str[-5:] if len(form_str) >= 5 else form_str
    pts = sum(3 if c == "W" else 1 if c == "D" else 0 for c in recent)
    return pts / (len(recent) * 3)

def analyze_injuries(injuries, team_id):
    key_positions = ["Goalkeeper", "Defender", "Midfielder", "Attacker"]
    key_players_out = 0
    for inj in injuries:
        player = inj.get("player") or {}
        team   = inj.get("team") or {}
        if team.get("id") != team_id:
            continue
        reason = (inj.get("reason") or "").lower()
        if "out" in reason or "injured" in reason or "questionable" in reason:
            pos = player.get("position") or ""
            if any(kp.lower() in pos.lower() for kp in key_positions):
                key_players_out += 1
    # Each key player out reduces prob by 3%
    return min(key_players_out * 0.03, 0.15)

def analyze_h2h(h2h_matches, home_team_id):
    if not h2h_matches:
        return 0.0
    home_wins = 0
    total     = 0
    for m in h2h_matches[-10:]:
        teams   = m.get("teams") or {}
        winner  = (m.get("score") or {})
        home_t  = teams.get("home") or {}
        away_t  = teams.get("away") or {}
        goals   = m.get("goals") or {}
        hg      = goals.get("home") or 0
        ag      = goals.get("away") or 0
        if hg is None or ag is None:
            continue
        total += 1
        if home_t.get("id") == home_team_id and hg > ag:
            home_wins += 1
        elif away_t.get("id") == home_team_id and ag > hg:
            home_wins += 1
    if total == 0:
        return 0.0
    return (home_wins / total) - 0.5

def calculate_professional_probs(fixture, prediction, injuries, h2h, home_stats, away_stats):
    teams   = fixture.get("teams") or {}
    home_t  = teams.get("home") or {}
    away_t  = teams.get("away") or {}
    home_id = home_t.get("id")
    away_id = away_t.get("id")

    # 1. Base probabilities from API-Football prediction (6 algorithms)
    pred_data = prediction.get("predictions") or {}
    percent   = pred_data.get("percent") or {}

    hp_base = to_f(percent.get("home") or percent.get("Home"))
    dp_base = to_f(percent.get("draw") or percent.get("Draw"))
    ap_base = to_f(percent.get("away") or percent.get("Away"))

    # Fallback to comparison if no percent
    if hp_base is None:
        comparison = prediction.get("comparison") or {}
        att_h = to_f((comparison.get("att") or {}).get("home")) or 0.5
        att_a = to_f((comparison.get("att") or {}).get("away")) or 0.5
        def_h = to_f((comparison.get("def") or {}).get("home")) or 0.5
        form_h = to_f((comparison.get("form") or {}).get("home")) or 0.5
        form_a = to_f((comparison.get("form") or {}).get("away")) or 0.5
        hp_base = (att_h * 0.4 + (1 - att_a) * 0.3 + form_h * 0.3)
        ap_base = (att_a * 0.4 + (1 - att_h) * 0.3 + form_a * 0.3)
        dp_base = 1.0 - hp_base - ap_base
        dp_base = max(dp_base, 0.10)

    if hp_base is None:
        hp_base, ap_base, dp_base = 0.45, 0.30, 0.25

    # 2. Form adjustment from standings
    home_standing = next((s for s in (prediction.get("teams") or {}).get("home", {}).values()
                          if isinstance(s, dict) and s.get("form")), {})
    away_standing = next((s for s in (prediction.get("teams") or {}).get("away", {}).values()
                          if isinstance(s, dict) and s.get("form")), {})

    home_form_score = form_string_to_score(
        (prediction.get("teams") or {}).get("home", {}).get("last_5", {}).get("form") or
        home_standing.get("form") or ""
    )
    away_form_score = form_string_to_score(
        (prediction.get("teams") or {}).get("away", {}).get("last_5", {}).get("form") or
        away_standing.get("form") or ""
    )

    form_adj = (home_form_score - away_form_score) * 0.08

    # 3. Injury adjustment
    home_inj_penalty = analyze_injuries(injuries, home_id)
    away_inj_penalty = analyze_injuries(injuries, away_id)
    injury_adj = away_inj_penalty - home_inj_penalty

    # 4. H2H adjustment
    h2h_adj = analyze_h2h(h2h, home_id) * 0.05

    # 5. Team stats adjustment (goals scored vs conceded)
    home_goals_avg = 1.4
    away_goals_avg = 1.1
    if home_stats:
        gs = home_stats.get("goals") or {}
        scored = (gs.get("for") or {}).get("average") or {}
        conceded = (gs.get("against") or {}).get("average") or {}
        home_goals_avg = float(scored.get("total") or scored.get("home") or 1.4)
        home_concede_avg = float(conceded.get("total") or conceded.get("home") or 1.1)
    else:
        home_goals_avg = 1.4
        home_concede_avg = 1.1

    if away_stats:
        gs = away_stats.get("goals") or {}
        scored = (gs.get("for") or {}).get("average") or {}
        conceded = (gs.get("against") or {}).get("average") or {}
        away_goals_avg = float(scored.get("total") or scored.get("away") or 1.1)
        away_concede_avg = float(conceded.get("total") or conceded.get("away") or 1.3)
    else:
        away_goals_avg = 1.1
        away_concede_avg = 1.3

    # Poisson expected goals
    lam_home = (home_goals_avg + away_concede_avg) / 2.0
    lam_away = (away_goals_avg + home_concede_avg) / 2.0
    lam_home = max(lam_home, 0.3)
    lam_away = max(lam_away, 0.3)

    # 6. Combine all adjustments
    total_adj = form_adj + injury_adj + h2h_adj
    hp_final = max(0.05, min(hp_base + total_adj, 0.90))
    ap_final = max(0.05, min(ap_base - total_adj * 0.5, 0.90))
    dp_final = max(0.05, 1.0 - hp_final - ap_final)

    nv = no_vig([hp_final, ap_final, dp_final])

    # Market probabilities via Poisson
    over25_p  = poisson_over(lam_home, lam_away, 2.5)
    under25_p = 1.0 - over25_p
    over15_p  = poisson_over(lam_home, lam_away, 1.5)
    under15_p = 1.0 - over15_p
    btts_p    = poisson_btts(lam_home, lam_away)

    # Confidence score (how many data sources confirmed)
    confidence_factors = []
    if prediction:
        confidence_factors.append("prediccion_api")
    if injuries:
        confidence_factors.append("lesiones")
    if h2h:
        confidence_factors.append("h2h")
    if home_stats or away_stats:
        confidence_factors.append("estadisticas")
    if home_form_score != 0.5 or away_form_score != 0.5:
        confidence_factors.append("forma")

    confidence_pct = min(50 + len(confidence_factors) * 10, 95)

    return {
        "home_p":       round(nv[0], 4),
        "away_p":       round(nv[1], 4),
        "draw_p":       round(nv[2], 4),
        "btts_p":       btts_p,
        "over25_p":     over25_p,
        "under25_p":    under25_p,
        "over15_p":     over15_p,
        "under15_p":    under15_p,
        "lam_home":     round(lam_home, 2),
        "lam_away":     round(lam_away, 2),
        "form_adj":     round(form_adj, 3),
        "injury_adj":   round(injury_adj, 3),
        "h2h_adj":      round(h2h_adj, 3),
        "home_injuries": int(home_inj_penalty / 0.03),
        "away_injuries": int(away_inj_penalty / 0.03),
        "data_confidence": confidence_pct,
        "factors_used":  confidence_factors,
    }

#  PARSE API-FOOTBALL FIXTURE 

def parse_apifootball_fixture(fixture_data, prediction, injuries, h2h,
                               home_stats, away_stats):
    fix    = fixture_data.get("fixture") or {}
    teams  = fixture_data.get("teams") or {}
    league = fixture_data.get("league") or {}
    home_t = teams.get("home") or {}
    away_t = teams.get("away") or {}

    home = home_t.get("name") or ""
    away = away_t.get("name") or ""
    if not home or not away:
        return None

    start = fix.get("date") or ""
    if not is_valid_date(start):
        return None

    probs = calculate_professional_probs(
        fixture_data, prediction, injuries, h2h, home_stats, away_stats
    )

    # Recommended bet from API prediction
    pred_data   = prediction.get("predictions") or {}
    api_advice  = pred_data.get("advice") or ""
    api_winner  = (pred_data.get("winner") or {}).get("name") or ""

    return {
        "id":            str(fix.get("id") or ""),
        "fixture_id":    fix.get("id"),
        "home":          home,
        "away":          away,
        "home_id":       home_t.get("id"),
        "away_id":       away_t.get("id"),
        "sport":         "soccer",
        "league":        league.get("name") or "",
        "league_id":     league.get("id"),
        "start":         start,
        "home_p":        probs["home_p"],
        "away_p":        probs["away_p"],
        "draw_p":        probs["draw_p"],
        "btts_p":        probs["btts_p"],
        "over25_p":      probs["over25_p"],
        "under25_p":     probs["under25_p"],
        "over15_p":      probs["over15_p"],
        "under15_p":     probs["under15_p"],
        "lam_home":      probs["lam_home"],
        "lam_away":      probs["lam_away"],
        "form_adj":      probs["form_adj"],
        "injury_adj":    probs["injury_adj"],
        "h2h_adj":       probs["h2h_adj"],
        "home_injuries": probs["home_injuries"],
        "away_injuries": probs["away_injuries"],
        "data_confidence": probs["data_confidence"],
        "factors_used":  probs["factors_used"],
        "api_advice":    api_advice,
        "api_winner":    api_winner,
        "api_home_odds": None,
        "api_draw_odds": None,
        "api_away_odds": None,
        "source":        "API-Football-Pro",
    }

#  ESPN FALLBACK 

ESPN_ENDPOINTS = [
    ("basketball", "nba",          "NBA"),
    ("football",   "nfl",          "NFL"),
    ("baseball",   "mlb",          "MLB"),
    ("hockey",     "nhl",          "NHL"),
    ("soccer",     "eng.1",        "Premier League"),
    ("soccer",     "esp.1",        "La Liga"),
    ("soccer",     "ger.1",        "Bundesliga"),
    ("soccer",     "ita.1",        "Serie A"),
    ("soccer",     "fra.1",        "Ligue 1"),
    ("soccer",     "usa.1",        "MLS"),
    ("soccer",     "arg.1",        "Liga Argentina"),
    ("soccer",     "bra.1",        "Brasileirao"),
    ("soccer",     "uefa.champions","Champions League"),
    ("soccer",     "uefa.europa",  "Europa League"),
    ("soccer",     "mex.1",        "Liga MX"),
]

def fetch_espn_events():
    events = []
    today  = datetime.now(timezone.utc).strftime("%Y%m%d")
    for sport, slug, name in ESPN_ENDPOINTS:
        try:
            url = "https://site.api.espn.com/apis/site/v2/sports/{}/{}/scoreboard".format(sport, slug)
            r   = requests.get(url, params={"dates": today}, timeout=8)
            r.raise_for_status()
            for ev in r.json().get("events", []):
                parsed = parse_espn_event(ev, name, sport)
                if parsed:
                    events.append(parsed)
        except Exception as e:
            print("ESPN {}: {}".format(name, str(e)))
    return events

def parse_espn_event(ev, league_name, sport_type):
    comps  = ev.get("competitions", [{}])
    comp   = comps[0] if comps else {}
    status = comp.get("status", {}).get("type", {}).get("name", "")
    if status in ("STATUS_FINAL", "STATUS_IN_PROGRESS"):
        return None
    competitors = comp.get("competitors", [])
    home = next((c for c in competitors if c.get("homeAway") == "home"), {})
    away = next((c for c in competitors if c.get("homeAway") == "away"), {})
    hn   = (home.get("team") or {}).get("displayName") or ""
    an   = (away.get("team") or {}).get("displayName") or ""
    if not hn or not an:
        return None
    start = ev.get("date") or ""
    if not is_valid_date(start):
        return None

    def get_rec(c):
        recs = c.get("records") or []
        if recs:
            p = (recs[0].get("summary") or "0-0").split("-")
            return int(p[0]) if p else 0, int(p[1]) if len(p) > 1 else 0
        return 0, 0

    hw, hl = get_rec(home)
    aw, al = get_rec(away)
    hp_raw = hw / max(hw + hl, 1)
    ap_raw = aw / max(aw + al, 1)
    is_soc = sport_type == "soccer"

    if is_soc:
        dp = 0.28
        t  = hp_raw + ap_raw + dp
        hp, ap = hp_raw / t, ap_raw / t
        dp = dp / t
    else:
        t  = hp_raw + ap_raw
        hp = hp_raw / max(t, 0.01)
        ap = ap_raw / max(t, 0.01)
        dp = None

    lh = 1.4 if is_soc else None
    la = 1.0 if is_soc else None

    return {
        "id": ev.get("id") or "", "home": hn, "away": an,
        "sport": "soccer" if is_soc else sport_type,
        "league": league_name, "start": start,
        "home_p": round(hp, 4), "away_p": round(ap, 4),
        "draw_p": round(dp, 4) if dp is not None else None,
        "btts_p":    round(poisson_btts(lh, la), 4) if lh else None,
        "over25_p":  round(poisson_over(lh, la, 2.5), 4) if lh else None,
        "under25_p": round(1.0 - poisson_over(lh, la, 2.5), 4) if lh else None,
        "over15_p":  round(poisson_over(lh, la, 1.5), 4) if lh else None,
        "under15_p": round(1.0 - poisson_over(lh, la, 1.5), 4) if lh else None,
        "home_record": str(hw) + "-" + str(hl),
        "away_record": str(aw) + "-" + str(al),
        "data_confidence": 50,
        "factors_used": ["record"],
        "api_advice": "", "api_winner": "",
        "api_home_odds": None, "api_draw_odds": None, "api_away_odds": None,
        "source": "ESPN",
    }

#  ODDS API 

def fetch_all_odds():
    all_odds = {}
    for sport_key in ALL_SPORT_KEYS:
        mkt = "h2h,btts,totals,double_chance" if "soccer" in sport_key else "h2h,totals"
        try:
            r = requests.get(
                ODDS_BASE + "/sports/" + sport_key + "/odds",
                params={"apiKey": ODDS_API_KEY, "regions": "eu,uk,us,au",
                        "markets": mkt, "oddsFormat": "decimal"},
                timeout=10,
            )
            r.raise_for_status()
            for g in r.json():
                all_odds[g.get("id")] = g
        except Exception:
            pass
    return list(all_odds.values())

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
    hn = event["home"].lower()
    an = event["away"].lower()
    for g in odds_list:
        gh = (g.get("home_team") or "").lower()
        ga = (g.get("away_team") or "").lower()
        hm = any(w in gh or gh in w for w in hn.split() if len(w) > 3)
        am = any(w in ga or ga in w for w in an.split() if len(w) > 3)
        if hm or am:
            return g
    return None

#  MARKET ANALYSIS 

def analyze_markets(event, odds_game):
    signals = []
    bks = odds_game.get("bookmakers", []) if odds_game else []

    def add(label, market, outcome, prob, odds, mtype):
        if prob is None or prob <= 0:
            return
        if odds <= 1.01:
            return
        ev_val = calc_ev(prob, odds)
        signals.append({
            "label":       label,
            "market":      market,
            "outcome":     outcome,
            "market_type": mtype,
            "true_prob":   round(prob * 100, 1),
            "odds":        round(odds, 2),
            "ev_pct":      round(ev_val * 100, 1),
            "kelly_pct":   calc_kelly(prob, odds),
            "_prob_raw":   prob,
            "_odds_raw":   odds,
        })

    hp = event["home_p"]
    ap = event["away_p"]
    dp = event.get("draw_p")
    hn = event["home"]
    an = event["away"]

    def get_odds(bk_name, market, api_key):
        api_v = event.get(api_key)
        if api_v and api_v > 1.01:
            return api_v
        if bks:
            v = best_odds_for(bks, "h2h", bk_name)
            if v > 1.01:
                return v
        p = hp if "home" in api_key else (ap if "away" in api_key else dp)
        return round(1.0 / max(p or 0.01, 0.01) * 1.05, 2)

    ho = get_odds(hn, "h2h", "api_home_odds")
    ao = get_odds(an, "h2h", "api_away_odds")
    add(hn + " GANA", "h2h", hn, hp, ho, "1X2")
    add(an + " GANA", "h2h", an, ap, ao, "1X2")

    if dp is not None:
        do = get_odds("Draw", "h2h", "api_draw_odds")
        add("EMPATE", "h2h", "Draw", dp, do, "1X2")
        p1x = hp + dp
        px2 = ap + dp
        p12 = hp + ap
        o1x = best_odds_for(bks, "double_chance", "1X") if bks else round(1.0 / max(p1x, 0.01) * 1.03, 2)
        ox2 = best_odds_for(bks, "double_chance", "X2") if bks else round(1.0 / max(px2, 0.01) * 1.03, 2)
        o12 = best_odds_for(bks, "double_chance", "12") if bks else round(1.0 / max(p12, 0.01) * 1.03, 2)
        add(hn + " o Empate (1X)", "double_chance", "1X", p1x, o1x, "Doble Oportunidad")
        add(an + " o Empate (X2)", "double_chance", "X2", px2, ox2, "Doble Oportunidad")
        add(hn + " o " + an + " (12)", "double_chance", "12", p12, o12, "Doble Oportunidad")

    bp = event.get("btts_p")
    if bp is not None:
        bo  = best_odds_for(bks, "btts", "Yes") if bks else round(1.0 / max(bp, 0.01) * 1.05, 2)
        bno = best_odds_for(bks, "btts", "No")  if bks else round(1.0 / max(1.0 - bp, 0.01) * 1.05, 2)
        add("AMBOS MARCAN - Si", "btts", "Yes", bp,       bo,  "BTTS")
        add("AMBOS MARCAN - No", "btts", "No",  1.0 - bp, bno, "BTTS")

    op = event.get("over25_p")
    up = event.get("under25_p")
    if op is not None:
        oo = best_odds_for(bks, "totals", "Over")  if bks else round(1.0 / max(op, 0.01) * 1.05, 2)
        uo = best_odds_for(bks, "totals", "Under") if bks else round(1.0 / max(up, 0.01) * 1.05, 2)
        add("MAS DE 2.5 GOLES",   "totals", "Over",  op, oo, "Over/Under")
        add("MENOS DE 2.5 GOLES", "totals", "Under", up, uo, "Over/Under")

    op15 = event.get("over15_p")
    up15 = event.get("under15_p")
    if op15 is not None:
        add("MAS DE 1.5 GOLES",   "totals15", "Over",  op15, round(1.0 / max(op15, 0.01) * 1.05, 2), "Over/Under")
        add("MENOS DE 1.5 GOLES", "totals15", "Under", up15, round(1.0 / max(up15, 0.01) * 1.05, 2), "Over/Under")

    return signals

def select_best_signal(signals):
    valid = [s for s in signals
             if s["_prob_raw"] >= MIN_PROB
             and s["_odds_raw"] >= MIN_ODDS
             and s["ev_pct"] > MIN_EV * 100]
    if not valid:
        return None
    return max(valid, key=lambda x: x["ev_pct"])

#  COMBINADAS 

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
            c_odds = round(reduce(lambda a, b: a * b, odds_list), 2)
            c_prob = reduce(lambda a, b: a * b, prob_list)
            if c_odds < MIN_ODDS or c_prob < 0.28:
                continue
            c_ev = round((c_prob * c_odds) - 1.0, 4)
            if c_ev <= 0:
                continue
            avg_conf = sum(c.get("data_confidence", 50) for c in combo) / len(combo)
            combinadas.append({
                "picks":       [c["label"] + " (" + c["partido"] + ")" for c in combo],
                "partidos":    [c["partido"] for c in combo],
                "deportes":    list({c["sport"] for c in combo}),
                "odds_list":   odds_list,
                "cuota_total": c_odds,
                "prob_total":  round(c_prob * 100, 1),
                "ev_pct":      round(c_ev * 100, 1),
                "kelly_pct":   round(max(0, min(c_prob - (1 - c_prob) / max(c_odds - 1, 0.01), 0.05) * 0.15 * 100, 0), 2),
                "legs":        r,
                "avg_confidence": round(avg_conf),
            })
    combinadas.sort(key=lambda x: (x["ev_pct"] * x["avg_confidence"] / 100), reverse=True)
    return combinadas[:8]

def dedup_events(events):
    seen = {}
    priority = {"API-Football-Pro": 4, "FootballPredictionAPI": 3, "GameForecast": 2, "ESPN": 1, "OddsAPI": 0}
    for ev in events:
        key = ev["home"].lower()[:5] + ev["away"].lower()[:5]
        if key not in seen:
            seen[key] = ev
        else:
            ep = priority.get(ev.get("source", ""), 0)
            sp = priority.get(seen[key].get("source", ""), 0)
            if ep > sp:
                seen[key] = ev
    return list(seen.values())

#  MAIN ENDPOINT 

@app.route("/api/signals", methods=["GET"])
def get_signals():
    all_events   = []
    sources_used = []
    request_budget = {"used": 0, "limit": 90}

    # 1. API-Football professional fixtures
    fixtures = fetch_apifootball_fixtures()
    if fixtures:
        sources_used.append("API-Football (lesiones + H2H + predicciones)")
        for fix_data in fixtures:
            if request_budget["used"] >= request_budget["limit"]:
                break
            fix    = fix_data.get("fixture") or {}
            teams  = fix_data.get("teams") or {}
            league = fix_data.get("league") or {}
            fid    = fix.get("id")
            lid    = league.get("id")
            ht_id  = (teams.get("home") or {}).get("id")
            at_id  = (teams.get("away") or {}).get("id")

            if not fid:
                continue

            # Only top leagues to save request budget
            if lid and lid not in TOP_LEAGUES:
                continue

            prediction  = fetch_apifootball_prediction(fid)
            request_budget["used"] += 1
            injuries    = fetch_apifootball_injuries(fid)
            request_budget["used"] += 1
            h2h         = fetch_apifootball_h2h(ht_id, at_id, 10) if ht_id and at_id else []
            request_budget["used"] += 1
            home_stats  = fetch_team_stats(ht_id, lid) if ht_id and lid else {}
            request_budget["used"] += 1
            away_stats  = fetch_team_stats(at_id, lid) if at_id and lid else {}
            request_budget["used"] += 1

            parsed = parse_apifootball_fixture(
                fix_data, prediction, injuries, h2h, home_stats, away_stats
            )
            if parsed:
                all_events.append(parsed)

    # 2. ESPN fallback for other sports (NBA, NFL, etc.)
    espn = fetch_espn_events()
    all_events.extend(espn)
    if espn:
        sources_used.append("ESPN (NBA, NFL, MLB, NHL)")

    all_events = dedup_events(all_events)

    # 3. Odds API
    odds_list = fetch_all_odds()
    if odds_list:
        sources_used.append("The Odds API (" + str(len(ALL_SPORT_KEYS)) + " ligas)")

    # Fallback from pure odds
    if not all_events:
        for g in odds_list:
            start = g.get("commence_time") or ""
            if not is_valid_date(start):
                continue
            all_events.append({
                "id": g.get("id", ""), "home": g.get("home_team", ""),
                "away": g.get("away_team", ""), "sport": g.get("sport_key", ""),
                "league": g.get("sport_title", ""), "start": start,
                "home_p": 0.50, "away_p": 0.50, "draw_p": None,
                "btts_p": None, "over25_p": None, "under25_p": None,
                "over15_p": None, "under15_p": None,
                "data_confidence": 40, "factors_used": [],
                "api_advice": "", "api_winner": "",
                "api_home_odds": None, "api_draw_odds": None, "api_away_odds": None,
                "source": "OddsAPI",
            })

    # Build picks
    picks = []
    for event in all_events:
        odds_game = match_event_to_odds(event, odds_list)
        signals   = analyze_markets(event, odds_game)
        best      = select_best_signal(signals)
        if not best:
            continue
        bks = [b["title"] for b in (odds_game.get("bookmakers", []) if odds_game else [])[:6]]
        best.update({
            "partido":        event["away"] + " vs " + event["home"],
            "home":           event["home"],
            "away":           event["away"],
            "sport":          event["sport"],
            "league":         event["league"],
            "start":          format_date(event["start"]),
            "emoji":          sport_emoji(event["sport"]),
            "bookmakers":     bks,
            "source":         event.get("source", ""),
            "data_confidence":event.get("data_confidence", 50),
            "factors_used":   event.get("factors_used", []),
            "api_advice":     event.get("api_advice", ""),
            "api_winner":     event.get("api_winner", ""),
            "home_injuries":  event.get("home_injuries", 0),
            "away_injuries":  event.get("away_injuries", 0),
            "_prob_raw":      best["_prob_raw"],
            "_odds_raw":      best["_odds_raw"],
        })
        picks.append(best)

    picks.sort(key=lambda x: (x["ev_pct"] * x.get("data_confidence", 50) / 100), reverse=True)
    for i, p in enumerate(picks):
        p["numero"] = i + 1

    combinadas = build_combinadas(picks) if len(picks) >= 2 else []

    for p in picks:
        p.pop("_prob_raw", None)
        p.pop("_odds_raw", None)

    return jsonify({
        "success":            True,
        "timestamp":          datetime.now(timezone.utc).isoformat(),
        "total_events":       len(all_events),
        "total_picks":        len(picks),
        "total_combinadas":   len(combinadas),
        "api_requests_used":  request_budget["used"],
        "picks":              picks,
        "combinadas":         combinadas,
        "sources_used":       sources_used,
        "filters":            {"min_prob_pct": MIN_PROB * 100, "min_odds": MIN_ODDS},
    })

@app.route("/api/debug", methods=["GET"])
def debug():
    fixtures = fetch_apifootball_fixtures()
    espn     = fetch_espn_events()
    return jsonify({
        "apifootball_fixtures": len(fixtures),
        "espn_events":          len(espn),
        "sample_fixture":       fixtures[:1] if fixtures else [],
        "sample_espn":          [{"home": e["home"], "away": e["away"], "league": e["league"]} for e in espn[:3]],
    })

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "ts": datetime.now(timezone.utc).isoformat()})

if __name__ == "__main__":
    app.run(debug=True, port=5000)

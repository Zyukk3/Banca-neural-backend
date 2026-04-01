import os
from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import itertools
import math
from datetime import datetime, timezone, timedelta
from functools import reduce

app = Flask(__name__)
CORS(app)

# -- KEYS ---------------------------------------------------------------------
APIFOOTBALL_KEY  = os.environ.get("APIFOOTBALL_KEY", "")
RAPIDAPI_KEY     = os.environ.get("RAPIDAPI_KEY", "")
ODDS_API_KEY     = os.environ.get("ODDS_API_KEY", "")
ODDS_BASE        = "https://api.the-odds-api.com/v4"
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY", "")
GROQ_BASE        = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL       = "llama-3.1-8b-instant"  # 14,400 req/day free

AF_BASE  = "https://v3.football.api-sports.io"
NBA_BASE = "https://v2.nba.api-sports.io"
MLB_BASE = "https://v1.baseball.api-sports.io"

def get_af_headers():
    key = os.environ.get("APIFOOTBALL_KEY", "")
    return {"x-apisports-key": key, "x-rapidapi-host": "v3.football.api-sports.io", "x-rapidapi-key": key}

def get_nba_headers():
    key = os.environ.get("APIFOOTBALL_KEY", "")
    return {"x-apisports-key": key, "x-rapidapi-host": "v2.nba.api-sports.io", "x-rapidapi-key": key}

def get_mlb_headers():
    key = os.environ.get("APIFOOTBALL_KEY", "")
    return {"x-apisports-key": key, "x-rapidapi-host": "v1.baseball.api-sports.io", "x-rapidapi-key": key}

# -- REQUEST BUDGET ------------------------------------------------------------
# Football API: 100/day
# NBA API:      100/day
# NFL API:      100/day  
# MLB API:      100/day
# NHL API:      100/day
# Total:        500 requests/day across all sports

# 100 requests/day TOTAL shared across all 3 APIs
TOTAL_BUDGET = 100
BUDGET = {
    "football": {"used": 0, "limit": 50},  # 50 for football (deep analysis)
    "nba":      {"used": 0, "limit": 25},  # 25 for basketball
    "mlb":      {"used": 0, "limit": 25},  # 25 for baseball
}

MIN_PROB    = 0.55
MIN_ODDS    = 1.50
MIN_EV      = 0.0
DATE_WINDOW = 7
SEASON      = datetime.now(timezone.utc).year
# API-Football uses starting year of season (2024 for 2024/25)
AF_SEASON   = SEASON - 1 if datetime.now(timezone.utc).month < 7 else SEASON

# ALL leagues - no restrictions
TOP_LEAGUES = {
    39:  "Premier League",     40: "FA Cup",
    140: "La Liga",            141: "Copa del Rey",
    135: "Serie A",            136: "Coppa Italia",
    78:  "Bundesliga",         81: "DFB Pokal",
    61:  "Ligue 1",            66: "Coupe de France",
    2:   "Champions League",   3:  "Europa League",
    848: "Conference League",
    88:  "Eredivisie",         94: "Primeira Liga",
    203: "Super Lig",          262: "Liga MX",
    253: "MLS",                71: "Brasileirao",
    128: "Liga Argentina",     119: "Allsvenskan",
    113: "Eliteserien",        169: "Super League Greece",
    197: "Super League Switzerland",
    235: "Premier League Russia",
    218: "Belgian Pro League", 144: "Jupiler Pro League",
    207: "Scottish Premiership",
    307: "Saudi Pro League",   17:  "Carabao Cup",
    45:  "FA Cup",
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
    "tennis_atp_french_open", "tennis_wta_french_open",
]

# -- MATH ----------------------------------------------------------------------

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
    return [p / t for p in probs] if t > 0 else probs

def calc_ev(prob, odds):
    return round((prob * odds) - 1.0, 4)

def calc_kelly(prob, odds, fraction=0.25):
    b = odds - 1.0
    if b <= 0:
        return 0.0
    k = (b * prob - (1.0 - prob)) / b
    return round(max(0.0, min(k * fraction * 100.0, 5.0)), 2)

def poisson_prob(lam, k):
    try:
        return (math.exp(-lam) * (lam ** k)) / math.factorial(k)
    except Exception:
        return 0.0

def poisson_over(lh, la, line=2.5):
    return round(sum(
        poisson_prob(lh, h) * poisson_prob(la, a)
        for h in range(9) for a in range(9) if h + a > line
    ), 4)

def poisson_btts(lh, la):
    return round((1.0 - poisson_prob(lh, 0)) * (1.0 - poisson_prob(la, 0)), 4)

def is_upcoming(ds):
    """Only matches that have NOT started yet. Strict - no past matches ever."""
    if not ds:
        return False
    try:
        d = datetime.fromisoformat(str(ds).replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        # Must be in the future (up to 7 days ahead)
        # No past matches - not even 1 minute ago
        return now < d <= now + timedelta(days=DATE_WINDOW)
    except Exception:
        return False

# Aliases for backward compat
def is_valid_date(ds):
    return is_upcoming(ds)

def is_today_or_future(ds):
    return is_upcoming(ds)

def fmt_date(iso):
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).strftime("%d/%m %H:%M UTC")
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
    if "american" in s or "nfl" in s or "football" in s:
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
        print("GET " + url + " ERROR: " + str(e))
        return {}

def use_budget(sport, n=1):
    b = BUDGET.get(sport, {})
    if b.get("used", 0) + n > b.get("limit", 0):
        return False
    b["used"] = b.get("used", 0) + n
    return True

# -- FOOTBALL API-SPORTS ENDPOINTS ---------------------------------------------

def af_get(endpoint, params):
    return safe_get(AF_BASE + endpoint, get_af_headers(), params)

def fetch_fixtures_for_date(target_date: str):
    """Fetch NOT STARTED fixtures for a specific date (YYYY-MM-DD)."""
    results = []
    seen_ids = set()
    now = datetime.now(timezone.utc)
    data = af_get("/fixtures", {"date": target_date, "status": "NS"})
    for fix in (data.get("response") or []):
        fid   = (fix.get("fixture") or {}).get("id")
        start = (fix.get("fixture") or {}).get("date") or ""
        if not fid or fid in seen_ids:
            continue
        # For future dates allow all NS fixtures
        # For today only allow ones that haven't started
        try:
            d = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            if d < now - timedelta(minutes=30):
                continue  # skip matches that already started
        except Exception:
            pass
        seen_ids.add(fid)
        results.append(fix)
    return results

def fetch_fixtures_today():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return fetch_fixtures_for_date(today)

def fetch_prediction(fid):
    if not use_budget("football"):
        return {}
    data = af_get("/predictions", {"fixture": fid})
    resp = data.get("response") or []
    return resp[0] if resp else {}

def fetch_injuries(fid):
    if not use_budget("football"):
        return []
    return af_get("/injuries", {"fixture": fid}).get("response") or []

def fetch_h2h(h_id, a_id):
    if not use_budget("football"):
        return []
    data = af_get("/fixtures/headtohead", {
        "h2h": str(h_id) + "-" + str(a_id), "last": 10
    })
    return data.get("response") or []

def fetch_team_stats(team_id, league_id):
    if not use_budget("football"):
        return {}
    data = af_get("/teams/statistics", {
        "team": team_id, "league": league_id, "season": AF_SEASON
    })
    return data.get("response") or {}

def fetch_lineups(fid):
    if not use_budget("football"):
        return []
    return af_get("/fixtures/lineups", {"fixture": fid}).get("response") or []

def fetch_fixture_stats(fid):
    if not use_budget("football"):
        return []
    return af_get("/fixtures/statistics", {"fixture": fid}).get("response") or []

# -- NBA API-SPORTS ------------------------------------------------------------

def fetch_nba_games_today(target_date: str = ""):
    today = target_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Use only 2 requests: today + tomorrow
    if not use_budget("nba", 2):
        return []
    games = []
    for delta in range(0, 2):
        day = (datetime.now(timezone.utc) + timedelta(days=delta)).strftime("%Y-%m-%d")
        data = safe_get(NBA_BASE + "/games", get_nba_headers(), {"date": day})
        resp = data.get("response") or []
        if not resp:
            data = safe_get(NBA_BASE + "/games", get_nba_headers(), {"date": day, "season": AF_SEASON})
            resp = data.get("response") or []
        games.extend(resp)
    events = []
    for g in games:
        status = (g.get("status") or {}).get("short") or ""
        if status in ("FT", "AOT", "HT", "Q1", "Q2", "Q3", "Q4"):
            continue
        teams  = g.get("teams") or {}
        home_t = teams.get("home") or {}
        away_t = teams.get("away") or {}
        hn = home_t.get("name") or ""
        an = away_t.get("name") or ""
        if not hn or not an:
            continue
        start = g.get("date") or ""
        if not is_today_or_future(start):
            continue

        # Win% based probability
        h_wins = home_t.get("statistics", [{}])[0].get("wins", {}).get("total") or 0 if home_t.get("statistics") else 0
        h_loss = home_t.get("statistics", [{}])[0].get("losses", {}).get("total") or 0 if home_t.get("statistics") else 0
        a_wins = away_t.get("statistics", [{}])[0].get("wins", {}).get("total") or 0 if away_t.get("statistics") else 0
        a_loss = away_t.get("statistics", [{}])[0].get("losses", {}).get("total") or 0 if away_t.get("statistics") else 0
        h_pct = h_wins / max(h_wins + h_loss, 1)
        a_pct = a_wins / max(a_wins + a_loss, 1)
        total_pct = h_pct + a_pct
        if total_pct > 0.01:
            hp = (h_pct / total_pct) * 0.95 + 0.58 * 0.05
            ap = 1.0 - hp
        else:
            hp = 0.58
            ap = 0.42
        events.append({
            "id": str(g.get("id") or ""), "home": hn, "away": an,
            "sport": "basketball", "league": "NBA", "start": start,
            "home_p": hp, "away_p": ap, "draw_p": None,
            "btts_p": None, "over25_p": None, "under25_p": None,
            "over15_p": None, "under15_p": None,
            "data_confidence": 65, "factors_used": ["nba_api", "home_advantage"],
            "api_advice": "", "api_winner": "",
            "api_home_odds": None, "api_draw_odds": None, "api_away_odds": None,
            "source": "NBA-API",
        })
    return events


# -- MLB API-SPORTS ------------------------------------------------------------

def fetch_mlb_games_today(target_date: str = ""):
    today = target_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Use only 2 requests: today + tomorrow
    if not use_budget("mlb", 2):
        return []
    games = []
    for delta in range(0, 2):
        day = (datetime.now(timezone.utc) + timedelta(days=delta)).strftime("%Y-%m-%d")
        data = safe_get(MLB_BASE + "/games", get_mlb_headers(), {"date": day})
        resp = data.get("response") or []
        if not resp:
            data = safe_get(MLB_BASE + "/games", get_mlb_headers(), {"date": day, "season": AF_SEASON})
            resp = data.get("response") or []
        games.extend(resp)
    events = []
    for g in games:
        status = (g.get("status") or {}).get("short") or ""
        if status in ("FT", "AOT"):
            continue
        teams  = g.get("teams") or {}
        home_t = teams.get("home") or {}
        away_t = teams.get("away") or {}
        hn = home_t.get("name") or ""
        an = away_t.get("name") or ""
        if not hn or not an:
            continue
        start = g.get("date") or ""
        if not is_today_or_future(start):
            continue
        events.append({
            "id": str(g.get("id") or ""),
            "home": hn, "away": an,
            "sport": "baseball", "league": "MLB", "start": start,
            "home_p": 0.54, "away_p": 0.46, "draw_p": None,
            "btts_p": None, "over25_p": None, "under25_p": None,
            "over15_p": None, "under15_p": None,
            "data_confidence": 60, "factors_used": ["mlb_api"],
            "api_advice": "", "api_winner": "",
            "api_home_odds": None, "api_draw_odds": None, "api_away_odds": None,
            "source": "MLB-API",
        })
    return events


# -- PROFESSIONAL PROBABILITY ENGINE ------------------------------------------

def form_to_score(form_str):
    if not form_str:
        return 0.5
    recent = form_str[-5:]
    pts = sum(3 if c == "W" else 1 if c == "D" else 0 for c in recent)
    return pts / (len(recent) * 3)

def injury_penalty(injuries, team_id):
    count = sum(
        1 for inj in injuries
        if (inj.get("team") or {}).get("id") == team_id
        and "out" in (inj.get("reason") or "").lower()
    )
    return min(count * 0.03, 0.15)

def h2h_advantage(h2h_matches, home_id):
    wins, total = 0, 0
    for m in h2h_matches[-10:]:
        teams = m.get("teams") or {}
        goals = m.get("goals") or {}
        hg, ag = goals.get("home"), goals.get("away")
        if hg is None or ag is None:
            continue
        total += 1
        ht_id = (teams.get("home") or {}).get("id")
        at_id = (teams.get("away") or {}).get("id")
        if ht_id == home_id and hg > ag:
            wins += 1
        elif at_id == home_id and ag > hg:
            wins += 1
    return ((wins / total) - 0.5) * 0.06 if total > 0 else 0.0

def lineup_strength(lineups, team_id):
    for lu in lineups:
        if (lu.get("team") or {}).get("id") == team_id:
            formation = lu.get("formation") or ""
            players   = lu.get("startXI") or []
            starters  = len(players)
            # Full lineup confirmed = higher confidence
            return {"confirmed": starters >= 11, "formation": formation, "starters": starters}
    return {"confirmed": False, "formation": "", "starters": 0}

def calculate_probs(fixture_data, prediction, injuries, h2h,
                    home_stats, away_stats, lineups):
    teams  = fixture_data.get("teams") or {}
    home_t = teams.get("home") or {}
    away_t = teams.get("away") or {}
    home_id = home_t.get("id")
    away_id = away_t.get("id")

    # 1. Base from API prediction (6 algorithms)
    pred_data = prediction.get("predictions") or {}
    pct       = pred_data.get("percent") or {}
    hp_base   = to_f(pct.get("home") or pct.get("Home"))
    dp_base   = to_f(pct.get("draw") or pct.get("Draw"))
    ap_base   = to_f(pct.get("away") or pct.get("Away"))

    if hp_base is None:
        comp   = prediction.get("comparison") or {}
        att_h  = to_f((comp.get("att") or {}).get("home")) or 0.5
        att_a  = to_f((comp.get("att") or {}).get("away")) or 0.5
        form_h = to_f((comp.get("form") or {}).get("home")) or 0.5
        form_a = to_f((comp.get("form") or {}).get("away")) or 0.5
        hp_base = att_h * 0.4 + (1 - att_a) * 0.3 + form_h * 0.3
        ap_base = att_a * 0.4 + (1 - att_h) * 0.3 + form_a * 0.3
        dp_base = max(1.0 - hp_base - ap_base, 0.10)

    if hp_base is None:
        hp_base, ap_base, dp_base = 0.45, 0.28, 0.27

    # 2. Form adjustment
    pred_teams = prediction.get("teams") or {}
    home_form  = form_to_score(((pred_teams.get("home") or {}).get("last_5") or {}).get("form") or "")
    away_form  = form_to_score(((pred_teams.get("away") or {}).get("last_5") or {}).get("form") or "")
    form_adj   = (home_form - away_form) * 0.08

    # 3. Injury adjustment
    home_pen = injury_penalty(injuries, home_id)
    away_pen = injury_penalty(injuries, away_id)
    inj_adj  = away_pen - home_pen

    # 4. H2H adjustment
    h2h_adj = h2h_advantage(h2h, home_id)

    # 5. Goals stats via Poisson
    def get_avg(stats, key, sub, fallback):
        try:
            v = stats.get("goals", {}).get(key, {}).get("average", {})
            val = v.get(sub) or v.get("total")
            return float(val) if val else fallback
        except Exception:
            return fallback

    lam_h = (get_avg(home_stats, "for",     "home",  1.4) +
             get_avg(away_stats, "against",  "away",  1.2)) / 2.0
    lam_a = (get_avg(away_stats, "for",     "away",  1.0) +
             get_avg(home_stats, "against", "home",  1.3)) / 2.0
    lam_h = max(lam_h, 0.3)
    lam_a = max(lam_a, 0.3)

    # 6. Lineup confirmation bonus
    home_lu = lineup_strength(lineups, home_id)
    away_lu = lineup_strength(lineups, away_id)
    lu_adj  = 0.02 if home_lu["confirmed"] else 0.0  # bonus only, no penalty if not confirmed

    # 7. Combine
    total_adj = form_adj + inj_adj + h2h_adj + lu_adj
    hp = max(0.05, min(hp_base + total_adj, 0.92))
    ap = max(0.05, min(ap_base - total_adj * 0.5, 0.92))
    dp = max(0.05, 1.0 - hp - ap)
    nv = no_vig([hp, ap, dp])

    # 8. Market probs
    over25  = poisson_over(lam_h, lam_a, 2.5)
    over15  = poisson_over(lam_h, lam_a, 1.5)
    over35  = poisson_over(lam_h, lam_a, 3.5)
    btts    = poisson_btts(lam_h, lam_a)

    # 9. Confidence score
    factors = []
    if prediction:                      factors.append("prediccion_6_algoritmos")
    if injuries:                        factors.append("lesiones_confirmadas")
    if h2h:                             factors.append("h2h_historico")
    if home_stats or away_stats:        factors.append("estadisticas_temporada")
    if home_form != 0.5:                factors.append("forma_reciente")
    if home_lu["confirmed"]:            factors.append("alineacion_confirmada")

    confidence = min(50 + len(factors) * 8, 96)

    return {
        "home_p":    round(nv[0], 4),
        "away_p":    round(nv[1], 4),
        "draw_p":    round(nv[2], 4),
        "btts_p":    btts,
        "over25_p":  over25,
        "under25_p": round(1.0 - over25, 4),
        "over15_p":  over15,
        "under15_p": round(1.0 - over15, 4),
        "over35_p":  over35,
        "under35_p": round(1.0 - over35, 4),
        "lam_home":  round(lam_h, 2),
        "lam_away":  round(lam_a, 2),
        "form_adj":  round(form_adj, 3),
        "inj_adj":   round(inj_adj, 3),
        "h2h_adj":   round(h2h_adj, 3),
        "home_injuries": int(home_pen / 0.03),
        "away_injuries": int(away_pen / 0.03),
        "home_formation": home_lu["formation"],
        "away_formation": away_lu["formation"],
        "lineup_confirmed": home_lu["confirmed"] and away_lu["confirmed"],
        "data_confidence": confidence,
        "factors_used": factors,
    }

def parse_football_fixture(fix_data, prediction, injuries, h2h,
                            home_stats, away_stats, lineups):
    fix    = fix_data.get("fixture") or {}
    teams  = fix_data.get("teams") or {}
    league = fix_data.get("league") or {}
    home_t = teams.get("home") or {}
    away_t = teams.get("away") or {}
    home   = home_t.get("name") or ""
    away   = away_t.get("name") or ""
    if not home or not away:
        return None
    start = fix.get("date") or ""
    if not is_today_or_future(start):
        return None

    probs = calculate_probs(fix_data, prediction, injuries, h2h,
                            home_stats, away_stats, lineups)
    pred_data  = prediction.get("predictions") or {}
    api_advice = pred_data.get("advice") or ""
    api_winner = (pred_data.get("winner") or {}).get("name") or ""

    # Build event context for Groq
    event_ctx = {
        "home": home, "away": away,
        "sport": "soccer", "league": league.get("name") or "",
        "home_injuries":  probs["home_injuries"],
        "away_injuries":  probs["away_injuries"],
        "home_formation": probs["home_formation"],
        "away_formation": probs["away_formation"],
        "api_advice":     api_advice,
        "api_winner":     api_winner,
    }
    # Groq analysis (35% weight)
    groq_result = groq_analyze(event_ctx, probs)
    probs       = blend_probs(probs, groq_result, groq_weight=0.35)

    return {
        "id":               str(fix.get("id") or ""),
        "fixture_id":       fix.get("id"),
        "home":             home,
        "away":             away,
        "home_id":          home_t.get("id"),
        "away_id":          away_t.get("id"),
        "sport":            "soccer",
        "league":           league.get("name") or "",
        "league_id":        league.get("id"),
        "start":            start,
        "home_p":           probs["home_p"],
        "away_p":           probs["away_p"],
        "draw_p":           probs["draw_p"],
        "btts_p":           probs["btts_p"],
        "over25_p":         probs["over25_p"],
        "under25_p":        probs["under25_p"],
        "over15_p":         probs["over15_p"],
        "under15_p":        probs["under15_p"],
        "over35_p":         probs["over35_p"],
        "under35_p":        probs["under35_p"],
        "lam_home":         probs["lam_home"],
        "lam_away":         probs["lam_away"],
        "home_injuries":    probs["home_injuries"],
        "away_injuries":    probs["away_injuries"],
        "home_formation":   probs["home_formation"],
        "away_formation":   probs["away_formation"],
        "lineup_confirmed": probs["lineup_confirmed"],
        "data_confidence":  probs["data_confidence"],
        "factors_used":     probs["factors_used"],
        "api_advice":       api_advice,
        "api_winner":       api_winner,
        "api_home_odds":    None,
        "api_draw_odds":    None,
        "api_away_odds":    None,
        "groq_usado":       probs.get("groq_usado", False),
        "groq_confianza":   probs.get("groq_confianza", 0),
        "groq_recs":        probs.get("groq_recs", ""),
        "groq_razon":       probs.get("groq_razon", ""),
        "groq_factores":    probs.get("groq_factores", []),
        "source":           "API-Football-Pro",
    }

# -- ESPN FALLBACK -------------------------------------------------------------

ESPN_EPS = [
    ("basketball", "nba",           "NBA"),
    ("baseball",   "mlb",           "MLB"),
    ("soccer",     "eng.1",         "Premier League"),
    ("soccer",     "esp.1",         "La Liga"),
    ("soccer",     "ger.1",         "Bundesliga"),
    ("soccer",     "ita.1",         "Serie A"),
    ("soccer",     "fra.1",         "Ligue 1"),
    ("soccer",     "usa.1",         "MLS"),
    ("soccer",     "arg.1",         "Liga Argentina"),
    ("soccer",     "bra.1",         "Brasileirao"),
    ("soccer",     "uefa.champions","Champions League"),
    ("soccer",     "uefa.europa",   "Europa League"),
    ("soccer",     "mex.1",         "Liga MX"),
    ("soccer",     "ned.1",         "Eredivisie"),
    ("soccer",     "por.1",         "Primeira Liga"),
]

def fetch_espn_events(target_date: str = ""):
    events = []
    if target_date:
        today = target_date.replace("-", "")
    else:
        today  = datetime.now(timezone.utc).strftime("%Y%m%d")
    for sport, slug, name in ESPN_EPS:
        try:
            url = "https://site.api.espn.com/apis/site/v2/sports/{}/{}/scoreboard".format(sport, slug)
            r   = requests.get(url, params={"dates": today}, timeout=8)
            r.raise_for_status()
            for ev in r.json().get("events", []):
                parsed = _parse_espn(ev, name, sport)
                if parsed:
                    events.append(parsed)
        except Exception as e:
            print("ESPN " + name + ": " + str(e))
    return events

def _parse_espn(ev, league_name, sport_type):
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
    if not is_today_or_future(start):
        return None

    def rec(c):
        recs = c.get("records") or []
        if recs:
            p = (recs[0].get("summary") or "0-0").split("-")
            return int(p[0]) if p else 0, int(p[1]) if len(p) > 1 else 0
        return 0, 0

    hw, hl = rec(home)
    aw, al = rec(away)
    hp_raw = hw / max(hw + hl, 1)
    ap_raw = aw / max(aw + al, 1)
    is_soc = sport_type == "soccer"

    if is_soc:
        dp = 0.28
        t  = hp_raw + ap_raw + dp
        hp, ap, dp = hp_raw / t, ap_raw / t, dp / t
    else:
        t  = hp_raw + ap_raw or 1
        hp, ap, dp = hp_raw / t, ap_raw / t, None

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
        "under25_p": round(1 - poisson_over(lh, la, 2.5), 4) if lh else None,
        "over15_p":  round(poisson_over(lh, la, 1.5), 4) if lh else None,
        "under15_p": round(1 - poisson_over(lh, la, 1.5), 4) if lh else None,
        "over35_p":  round(poisson_over(lh, la, 3.5), 4) if lh else None,
        "under35_p": round(1 - poisson_over(lh, la, 3.5), 4) if lh else None,
        "home_record": str(hw) + "-" + str(hl),
        "away_record": str(aw) + "-" + str(al),
        "data_confidence": 55,
        "factors_used": ["record_temporada"],
        "api_advice": "", "api_winner": "",
        "api_home_odds": None, "api_draw_odds": None, "api_away_odds": None,
        "home_injuries": 0, "away_injuries": 0,
        "home_formation": "", "away_formation": "",
        "lineup_confirmed": False,
        "source": "ESPN",
    }

# -- ODDS API ------------------------------------------------------------------

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

def best_odds_for(bks, market_key, outcome_name):
    best = 1.01
    for bk in bks:
        for mkt in bk.get("markets", []):
            if mkt["key"] == market_key:
                for out in mkt.get("outcomes", []):
                    if outcome_name.lower() in (out.get("name") or "").lower():
                        best = max(best, out["price"])
    return best

def match_to_odds(event, odds_list):
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


# -- GROQ AI EXPERT ANALYSIS --------------------------------------------------

EXPERT_SYSTEM_PROMPT = """Eres un analista experto en pronosticos deportivos con 20 anos de experiencia.
Analizas partidos de futbol, basketball y baseball con metodologia profesional.
Tu rol es complementar el analisis estadistico con contexto cualitativo:
- Motivacion de los equipos (titulo, descenso, copa)
- Momento de forma real (mas alla de las estadisticas)
- Factores psicologicos (presion, revancha, local/visitante)
- Lesiones clave y su impacto real en el juego
- Historial directo y patrones de juego

IMPORTANTE: Tu analisis es UN FACTOR MAS dentro de un modelo estadistico.
Debes devolver SIEMPRE un JSON valido con este formato exacto:
{
  "prob_home": 0.XX,
  "prob_away": 0.XX,
  "prob_draw": 0.XX,
  "confianza": 0.XX,
  "recomendacion": "HOME|AWAY|DRAW|NO_BET",
  "razonamiento": "texto breve maximo 2 oraciones",
  "factores_clave": ["factor1", "factor2", "factor3"]
}
prob_home + prob_away + prob_draw deben sumar 1.0
confianza entre 0.0 y 1.0 (que tan seguro estas del analisis)
NO incluyas texto fuera del JSON."""

def groq_analyze(event: dict, stats_probs: dict) -> dict:
    """
    Llama a Groq con contexto del partido y retorna analisis de IA.
    Si falla, retorna dict vacio para que el modelo estadistico tome control.
    """
    home = event.get("home", "")
    away = event.get("away", "")
    league = event.get("league", "")
    sport = event.get("sport", "")

    h_inj = event.get("home_injuries", 0)
    a_inj = event.get("away_injuries", 0)
    h_form = event.get("home_formation", "")
    a_form = event.get("away_formation", "")

    stat_hp = stats_probs.get("home_p", 0.45)
    stat_ap = stats_probs.get("away_p", 0.30)
    stat_dp = stats_probs.get("draw_p", 0.25)
    lam_h   = stats_probs.get("lam_home", 1.4)
    lam_a   = stats_probs.get("lam_away", 1.1)

    user_msg = f"""Analiza este partido:

DEPORTE: {sport} | LIGA: {league}
PARTIDO: {away} vs {home} (local: {home})

ESTADISTICAS DEL MODELO:
- Prob victoria local ({home}): {round(stat_hp*100,1)}%
- Prob empate: {round(stat_dp*100,1)}%
- Prob victoria visitante ({away}): {round(stat_ap*100,1)}%
- Goles esperados local: {lam_h} | visitante: {lam_a}

CONTEXTO:
- Lesiones locales confirmadas: {h_inj} jugadores clave
- Lesiones visitante confirmadas: {a_inj} jugadores clave
- Formacion local: {h_form or "no confirmada"}
- Formacion visitante: {a_form or "no confirmada"}
- Consejo API-Football: {event.get("api_advice", "no disponible")}
- Ganador sugerido por API: {event.get("api_winner", "no disponible")}

Complementa el analisis estadistico con tu experiencia. Devuelve SOLO el JSON."""

    try:
        resp = requests.post(
            GROQ_BASE,
            headers={
                "Authorization": "Bearer " + GROQ_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": EXPERT_SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                "max_tokens": 300,
                "temperature": 0.3,  # low temp = consistent, analytical
            },
            timeout=8,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()

        # Parse JSON from response
        import json as _json
        # Clean any markdown fences
        content = content.replace("```json", "").replace("```", "").strip()
        result = _json.loads(content)

        # Validate required fields
        required = ["prob_home", "prob_away", "confianza", "recomendacion", "razonamiento"]
        if not all(k in result for k in required):
            return {}

        # Normalize probabilities
        total = result["prob_home"] + result["prob_away"] + result.get("prob_draw", 0)
        if total > 0:
            result["prob_home"] = round(result["prob_home"] / total, 4)
            result["prob_away"] = round(result["prob_away"] / total, 4)
            result["prob_draw"] = round(result.get("prob_draw", 0) / total, 4)

        return result

    except Exception as e:
        print("Groq error: " + str(e))
        return {}


def blend_probs(stats: dict, groq: dict, groq_weight: float = 0.35) -> dict:
    """
    Combina probabilidades estadisticas (60%) con analisis Groq (40%).
    Si Groq falla, usa 100% estadistico.
    """
    if not groq:
        return stats

    stat_w = 1.0 - groq_weight

    blended_hp = stats["home_p"] * stat_w + groq["prob_home"] * groq_weight
    blended_ap = stats["away_p"] * stat_w + groq["prob_away"] * groq_weight
    blended_dp = stats.get("draw_p", 0) * stat_w + groq.get("prob_draw", 0) * groq_weight

    total = blended_hp + blended_ap + blended_dp
    if total > 0:
        blended_hp /= total
        blended_ap /= total
        blended_dp /= total

    result = dict(stats)
    result["home_p"]         = round(blended_hp, 4)
    result["away_p"]         = round(blended_ap, 4)
    result["draw_p"]         = round(blended_dp, 4) if blended_dp > 0 else None
    result["groq_confianza"] = groq.get("confianza", 0)
    result["groq_recs"]      = groq.get("recomendacion", "")
    result["groq_razon"]     = groq.get("razonamiento", "")
    result["groq_factores"]  = groq.get("factores_clave", [])
    result["groq_usado"]     = True

    return result

# -- MARKET ANALYSIS -----------------------------------------------------------

def analyze_markets(event, odds_game):
    signals = []
    bks = odds_game.get("bookmakers", []) if odds_game else []

    def add(label, market, outcome, prob, odds, mtype):
        if not prob or prob <= 0 or odds <= 1.01:
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

    def get_o(name, mkt, api_key, prob):
        api_v = event.get(api_key)
        if api_v and api_v > 1.01:
            return api_v
        if bks:
            v = best_odds_for(bks, mkt, name)
            if v > 1.01:
                return v
        return round(1.0 / max(prob, 0.01) * 1.05, 2)

    ho = get_o(hn, "h2h", "api_home_odds", hp)
    ao = get_o(an, "h2h", "api_away_odds", ap)
    add(hn + " GANA", "h2h", hn, hp, ho, "1X2")
    add(an + " GANA", "h2h", an, ap, ao, "1X2")

    if dp is not None:
        do  = get_o("Draw", "h2h", "api_draw_odds", dp)
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
    if bp:
        bo  = best_odds_for(bks, "btts", "Yes") if bks else round(1.0 / max(bp, 0.01) * 1.05, 2)
        bno = best_odds_for(bks, "btts", "No")  if bks else round(1.0 / max(1 - bp, 0.01) * 1.05, 2)
        add("AMBOS MARCAN - Si", "btts", "Yes", bp,     bo,  "BTTS")
        add("AMBOS MARCAN - No", "btts", "No",  1 - bp, bno, "BTTS")

    for line, label_o, label_u in [(2.5, "MAS DE 2.5 GOLES", "MENOS DE 2.5 GOLES"),
                                    (1.5, "MAS DE 1.5 GOLES", "MENOS DE 1.5 GOLES"),
                                    (3.5, "MAS DE 3.5 GOLES", "MENOS DE 3.5 GOLES")]:
        key_o = "over" + str(line).replace(".", "") + "_p"
        key_u = "under" + str(line).replace(".", "") + "_p"
        op = event.get(key_o)
        up = event.get(key_u)
        if op:
            oo = best_odds_for(bks, "totals", "Over")  if bks and line == 2.5 else round(1.0 / max(op, 0.01) * 1.05, 2)
            uo = best_odds_for(bks, "totals", "Under") if bks and line == 2.5 else round(1.0 / max(up or 0.01, 0.01) * 1.05, 2)
            add(label_o, "totals", "Over",  op, oo, "Over/Under")
            if up:
                add(label_u, "totals", "Under", up, uo, "Over/Under")

    return signals

def select_best(signals):
    valid = [s for s in signals
             if s["_prob_raw"] >= MIN_PROB
             and s["_odds_raw"] >= MIN_ODDS
             and s["ev_pct"] > 0]
    return max(valid, key=lambda x: x["ev_pct"]) if valid else None

# -- COMBINADAS ----------------------------------------------------------------

def build_combinadas(picks):
    combinadas = []
    pool = list({p["partido"]: p for p in picks}.values())
    if len(pool) < 2:
        return []
    for r in range(2, min(5, len(pool) + 1)):
        for combo in itertools.combinations(pool, r):
            c_odds = round(reduce(lambda a, b: a * b, [c["odds"] for c in combo]), 2)
            c_prob = reduce(lambda a, b: a * b, [c["_prob_raw"] for c in combo])
            if c_odds < MIN_ODDS or c_prob < 0.25:
                continue
            c_ev = round((c_prob * c_odds) - 1.0, 4)
            if c_ev <= 0:
                continue
            avg_conf = sum(c.get("data_confidence", 50) for c in combo) / len(combo)
            combinadas.append({
                "picks":       [c["label"] + " (" + c["partido"] + ")" for c in combo],
                "partidos":    [c["partido"] for c in combo],
                "deportes":    list({c["sport"] for c in combo}),
                "odds_list":   [c["odds"] for c in combo],
                "cuota_total": c_odds,
                "prob_total":  round(c_prob * 100, 1),
                "ev_pct":      round(c_ev * 100, 1),
                "kelly_pct":   round(max(0, min(c_prob - (1 - c_prob) / max(c_odds - 1, 0.01), 0.05) * 0.15 * 100, 0), 2),
                "legs":        r,
                "avg_confidence": round(avg_conf),
            })
    combinadas.sort(key=lambda x: x["ev_pct"] * x["avg_confidence"] / 100, reverse=True)
    return combinadas[:10]

def dedup(events):
    seen = {}
    pri  = {"API-Football-Pro": 5, "NBA-API": 4, "NFL-API": 4, "MLB-API": 4,
            "NHL-API": 4, "ESPN": 2, "OddsAPI": 1}
    for ev in events:
        key = ev["home"].lower()[:8] + "_" + ev["away"].lower()[:8]
        if key not in seen or pri.get(ev.get("source",""), 0) > pri.get(seen[key].get("source",""), 0):
            seen[key] = ev
    return list(seen.values())

# -- MAIN ENDPOINT -------------------------------------------------------------

@app.route("/api/signals", methods=["GET"])
def get_signals():
    global BUDGET
    BUDGET = {
        "football": {"used": 0, "limit": 50},
        "nba":      {"used": 0, "limit": 25},
        "mlb":      {"used": 0, "limit": 25},
    }

    # Date parameter - default to today UTC
    target_date = request.args.get("date", "")
    if not target_date:
        target_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Validate format
    try:
        datetime.strptime(target_date, "%Y-%m-%d")
    except ValueError:
        target_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    all_events   = []
    sources_used = []

    # 1. Football - full professional analysis
    fixtures = fetch_fixtures_for_date(target_date)
    BUDGET["football"]["used"] += 1  # for the fixtures call itself
    if fixtures:
        sources_used.append("API-Football (prediccion+lesiones+H2H+stats+lineups)")
        for fix_data in fixtures:
            fix    = fix_data.get("fixture") or {}
            teams  = fix_data.get("teams") or {}
            league = fix_data.get("league") or {}
            fid    = fix.get("id")
            lid    = league.get("id")
            ht_id  = (teams.get("home") or {}).get("id")
            at_id  = (teams.get("away") or {}).get("id")
            if not fid:
                continue
            # 5 requests per match: prediction, injuries, h2h, home_stats, away_stats
            # With 50 football budget: covers up to 8 matches deep analysis
            if BUDGET["football"]["used"] + 5 > BUDGET["football"]["limit"]:
                break
            # Skip if fixture already started
            fix_start = fix.get("date") or ""
            if not is_upcoming(fix_start):
                continue
            prediction  = fetch_prediction(fid)
            injuries    = fetch_injuries(fid)
            h2h         = fetch_h2h(ht_id, at_id) if ht_id and at_id else []
            home_stats  = fetch_team_stats(ht_id, lid) if ht_id and lid else {}
            away_stats  = fetch_team_stats(at_id, lid) if at_id and lid else {}
            lineups     = fetch_lineups(fid)
            parsed = parse_football_fixture(fix_data, prediction, injuries, h2h,
                                            home_stats, away_stats, lineups)
            if parsed:
                all_events.append(parsed)

    # 2. NBA Basketball
    nba = fetch_nba_games_today(target_date)
    if nba:
        all_events.extend(nba)
        sources_used.append("API-Sports NBA")

    # 3. MLB Baseball
    mlb = fetch_mlb_games_today(target_date)
    if mlb:
        all_events.extend(mlb)
        sources_used.append("API-Sports MLB")

    # 3. ESPN as extra coverage
    espn = fetch_espn_events(target_date)
    all_events.extend(espn)
    if espn:
        sources_used.append("ESPN (cobertura extra)")

    all_events = dedup(all_events)

    # 4. Odds API
    odds_list = fetch_all_odds()
    if odds_list:
        sources_used.append("The Odds API (" + str(len(ALL_SPORT_KEYS)) + " ligas)")

    # Fallback events from odds
    if not all_events:
        for g in odds_list:
            start = g.get("commence_time") or ""
            if not is_today_or_future(start):
                continue
            all_events.append({
                "id": g.get("id",""), "home": g.get("home_team",""),
                "away": g.get("away_team",""), "sport": g.get("sport_key",""),
                "league": g.get("sport_title",""), "start": start,
                "home_p": 0.50, "away_p": 0.50, "draw_p": None,
                "btts_p": None, "over25_p": None, "under25_p": None,
                "over15_p": None, "under15_p": None,
                "over35_p": None, "under35_p": None,
                "data_confidence": 40, "factors_used": [],
                "api_advice": "", "api_winner": "",
                "api_home_odds": None, "api_draw_odds": None, "api_away_odds": None,
                "home_injuries": 0, "away_injuries": 0,
                "home_formation": "", "away_formation": "",
                "lineup_confirmed": False, "source": "OddsAPI",
            })

    # 5. Build picks
    picks = []
    for event in all_events:
        odds_game = match_to_odds(event, odds_list)
        signals   = analyze_markets(event, odds_game)
        best      = select_best(signals)
        if not best:
            continue
        bks = [b["title"] for b in (odds_game.get("bookmakers",[]) if odds_game else [])[:6]]
        best.update({
            "partido":          event["away"] + " vs " + event["home"],
            "home":             event["home"],
            "away":             event["away"],
            "sport":            event["sport"],
            "league":           event["league"],
            "start":            fmt_date(event["start"]),
            "emoji":            sport_emoji(event["sport"]),
            "bookmakers":       bks,
            "source":           event.get("source",""),
            "data_confidence":  event.get("data_confidence", 50),
            "factors_used":     event.get("factors_used",[]),
            "api_advice":       event.get("api_advice",""),
            "api_winner":       event.get("api_winner",""),
            "home_injuries":    event.get("home_injuries",0),
            "away_injuries":    event.get("away_injuries",0),
            "home_formation":   event.get("home_formation",""),
            "away_formation":   event.get("away_formation",""),
            "lineup_confirmed": event.get("lineup_confirmed",False),
            "groq_usado":       event.get("groq_usado", False),
            "groq_confianza":   event.get("groq_confianza", 0),
            "groq_recs":        event.get("groq_recs", ""),
            "groq_razon":       event.get("groq_razon", ""),
            "groq_factores":    event.get("groq_factores", []),
            "_prob_raw":        best["_prob_raw"],
            "_odds_raw":        best["_odds_raw"],
        })
        picks.append(best)

    picks.sort(key=lambda x: x["ev_pct"] * x.get("data_confidence",50) / 100, reverse=True)
    for i, p in enumerate(picks):
        p["numero"] = i + 1

    combinadas = build_combinadas(picks) if len(picks) >= 2 else []

    for p in picks:
        p.pop("_prob_raw", None)
        p.pop("_odds_raw", None)

    total_requests = sum(b["used"] for b in BUDGET.values())

    return jsonify({
        "success":           True,
        "date_analyzed":     target_date,
        "timestamp":         datetime.now(timezone.utc).isoformat(),
        "total_events":      len(all_events),
        "total_picks":       len(picks),
        "total_combinadas":  len(combinadas),
        "requests_used":     total_requests,
        "budget_detail":     {k: v for k, v in BUDGET.items()},
        "picks":             picks,
        "combinadas":        combinadas,
        "sources_used":      sources_used,
        "filters":           {"min_prob_pct": MIN_PROB * 100, "min_odds": MIN_ODDS},
    })

@app.route("/api/debug", methods=["GET"])
def debug():
    fixtures = fetch_fixtures_today()
    espn     = fetch_espn_events()
    # Test each sport API
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    nba_test = safe_get(NBA_BASE + "/games", get_nba_headers(), {"date": today})
    mlb_test = safe_get(MLB_BASE + "/games", get_mlb_headers(), {"date": today})
    return jsonify({
        "football_fixtures": len(fixtures),
        "espn_events":       len(espn),
        "budget_used":       {k: v["used"] for k, v in BUDGET.items()},
        "budget_limit":      {k: v["limit"] for k, v in BUDGET.items()},
        "total_budget_100":  sum(v["used"] for v in BUDGET.values()),
        "api_tests": {
            "football": {"fixtures": len(fixtures)},
            "nba": {"count": len(nba_test.get("response") or []), "errors": nba_test.get("errors")},
            "mlb": {"count": len(mlb_test.get("response") or []), "errors": mlb_test.get("errors")},
        },
        "sample_fixture": fixtures[:1] if fixtures else [],
        "sample_espn": [{"home": e["home"], "away": e["away"], "league": e["league"], "start": e["start"]} for e in espn[:5]],
    })

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "ts": datetime.now(timezone.utc).isoformat()})

if __name__ == "__main__":
    app.run(debug=True, port=5000)

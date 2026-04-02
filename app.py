import os
import json
import math
import itertools
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from datetime import datetime, timezone, timedelta
from functools import reduce
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)
CORS(app)

# -- KEYS (from Render environment variables) ----------------------------------
def get_key(name):
    return os.environ.get(name, "")

AF_BASE   = "https://v3.football.api-sports.io"
NBA_BASE  = "https://v2.nba.api-sports.io"
MLB_BASE  = "https://v1.baseball.api-sports.io"
ODDS_BASE = "https://api.the-odds-api.com/v4"
GROQ_BASE = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.1-8b-instant"

def af_headers():
    # Only x-apisports-key needed when calling api-sports.io directly
    return {"x-apisports-key": get_key("APIFOOTBALL_KEY")}

def nba_headers():
    return {"x-apisports-key": get_key("APIFOOTBALL_KEY")}

def mlb_headers():
    return {"x-apisports-key": get_key("APIFOOTBALL_KEY")}

# -- CONFIG --------------------------------------------------------------------
MIN_PROB = 0.52
MIN_ODDS = 1.50
TIMEOUT  = 15  # seconds per HTTP request - Render needs time to wake up

# -- MATH ----------------------------------------------------------------------

def to_f(v):
    if v is None: return None
    try:
        f = float(v)
        return f / 100.0 if f > 1.0 else f
    except: return None

def no_vig(probs):
    t = sum(probs)
    return [p / t for p in probs] if t > 0 else probs

def calc_ev(prob, odds):
    return round((prob * odds) - 1.0, 4)

def calc_kelly(prob, odds, f=0.25):
    b = odds - 1.0
    if b <= 0: return 0.0
    k = (b * prob - (1.0 - prob)) / b
    return round(max(0.0, min(k * f * 100.0, 5.0)), 2)

def poisson_p(lam, k):
    try: return (math.exp(-lam) * (lam**k)) / math.factorial(k)
    except: return 0.0

def poisson_over(lh, la, line=2.5):
    return round(sum(
        poisson_p(lh,h)*poisson_p(la,a)
        for h in range(9) for a in range(9) if h+a > line
    ), 4)

def poisson_btts(lh, la):
    return round((1.0-poisson_p(lh,0))*(1.0-poisson_p(la,0)), 4)

def fmt_date(iso):
    if not iso: return ""
    try: return datetime.fromisoformat(str(iso).replace("Z","+00:00")).strftime("%d/%m %H:%M UTC")
    except: return str(iso)

def sport_icon(sport):
    s = (sport or "").lower()
    if "soccer" in s: return "soccer"
    if "basketball" in s or "nba" in s: return "basketball"
    if "baseball" in s: return "baseball"
    return "sport"

# -- HTTP ----------------------------------------------------------------------

def get_json(url, headers=None, params=None):
    try:
        r = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        # Log API-Football specific errors
        if "api-sports.io" in url or "api-football" in url:
            errs = data.get("errors") or {}
            if errs:
                print(f"API-SPORTS ERROR at {url}: {errs}")
        return data
    except Exception as e:
        print(f"GET {url} => {type(e).__name__}: {e}")
        return {}

# -- DATE HELPERS --------------------------------------------------------------

def parse_dt(ds):
    if not ds: return None
    try: return datetime.fromisoformat(str(ds).replace("Z","+00:00"))
    except: return None

def is_valid_fixture(start_str, target_date_str):
    """
    Accept fixture if its date matches target_date (YYYY-MM-DD in UTC).
    For today: only future matches. For future dates: all NS matches.
    """
    d = parse_dt(start_str)
    if not d: return False
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    fixture_day = d.strftime("%Y-%m-%d")

    if fixture_day != target_date_str:
        return False
    if target_date_str == today:
        return d > now - timedelta(minutes=5)
    return True

# -- API-FOOTBALL --------------------------------------------------------------

def fetch_fixtures(target_date):
    data = get_json(AF_BASE+"/fixtures", af_headers(),
                    {"date": target_date, "status": "NS"})
    errors  = data.get("errors") or {}
    results = data.get("results") or 0
    raw     = data.get("response") or []
    print(f"API-Football /fixtures -> results={results} errors={errors} raw_count={len(raw)}")
    if errors:
        print(f"API-Football ERROR: {errors}")
    fixtures = []
    for fix in raw:
        start = (fix.get("fixture") or {}).get("date") or ""
        if is_valid_fixture(start, target_date):
            fixtures.append(fix)
    print(f"Fixtures for {target_date} after filter: {len(fixtures)}")
    return fixtures

def fetch_prediction(fid):
    data = get_json(AF_BASE+"/predictions", af_headers(), {"fixture": fid})
    resp = data.get("response") or []
    return resp[0] if resp else {}

def fetch_injuries(fid):
    data = get_json(AF_BASE+"/injuries", af_headers(), {"fixture": fid})
    return data.get("response") or []

def fetch_h2h(h_id, a_id):
    data = get_json(AF_BASE+"/fixtures/headtohead", af_headers(),
                    {"h2h": f"{h_id}-{a_id}", "last": 8})
    return data.get("response") or []

def fetch_team_stats(team_id, league_id, season):
    data = get_json(AF_BASE+"/teams/statistics", af_headers(),
                    {"team": team_id, "league": league_id, "season": season})
    return data.get("response") or {}

def fetch_lineups(fid):
    data = get_json(AF_BASE+"/fixtures/lineups", af_headers(), {"fixture": fid})
    return data.get("response") or []

# -- PROBABILITY ENGINE --------------------------------------------------------

def form_score(form_str):
    if not form_str: return 0.5
    recent = form_str[-5:]
    pts = sum(3 if c=="W" else 1 if c=="D" else 0 for c in recent)
    return pts / (len(recent)*3)

def injury_pen(injuries, team_id):
    count = sum(1 for inj in injuries
                if (inj.get("team") or {}).get("id") == team_id
                and "out" in (inj.get("reason") or "").lower())
    return min(count*0.03, 0.12)

def h2h_adj(matches, home_id):
    wins, total = 0, 0
    for m in matches[-8:]:
        teams = m.get("teams") or {}
        goals = m.get("goals") or {}
        hg, ag = goals.get("home"), goals.get("away")
        if hg is None or ag is None: continue
        total += 1
        ht = (teams.get("home") or {}).get("id")
        at = (teams.get("away") or {}).get("id")
        if ht == home_id and hg > ag: wins += 1
        elif at == home_id and ag > hg: wins += 1
    return ((wins/total) - 0.5) * 0.05 if total > 0 else 0.0

def goals_avg(stats, direction, venue, fallback):
    try:
        v = (stats.get("goals") or {}).get(direction, {}).get("average", {})
        val = v.get(venue) or v.get("total")
        return float(val) if val else fallback
    except: return fallback

def lu_info(lineups, team_id):
    for lu in lineups:
        if (lu.get("team") or {}).get("id") == team_id:
            xi = lu.get("startXI") or []
            return {"ok": len(xi) >= 11, "formation": lu.get("formation") or ""}
    return {"ok": False, "formation": ""}

def calculate_probs(fix_data, pred, injuries, h2h, hs, as_, lineups):
    teams  = fix_data.get("teams") or {}
    home_t = teams.get("home") or {}
    away_t = teams.get("away") or {}
    hid, aid = home_t.get("id"), away_t.get("id")

    # Base probs from API prediction
    pct    = (pred.get("predictions") or {}).get("percent") or {}
    hp     = to_f(pct.get("home") or pct.get("Home"))
    dp     = to_f(pct.get("draw") or pct.get("Draw"))
    ap     = to_f(pct.get("away") or pct.get("Away"))

    if hp is None:
        comp  = pred.get("comparison") or {}
        att_h = to_f((comp.get("att") or {}).get("home")) or 0.5
        att_a = to_f((comp.get("att") or {}).get("away")) or 0.5
        hp = att_h*0.5 + 0.5*0.5
        ap = att_a*0.5 + 0.5*0.5
        dp = max(1.0-hp-ap, 0.10)
    if hp is None:
        hp, ap, dp = 0.45, 0.28, 0.27

    # Adjustments
    pred_t  = pred.get("teams") or {}
    hform   = form_score(((pred_t.get("home") or {}).get("last_5") or {}).get("form") or "")
    aform   = form_score(((pred_t.get("away") or {}).get("last_5") or {}).get("form") or "")
    form_a  = (hform - aform) * 0.07
    inj_a   = injury_pen(injuries, aid) - injury_pen(injuries, hid)
    h2h_a   = h2h_adj(h2h, hid)

    lam_h = (goals_avg(hs, "for",     "home", 1.4) + goals_avg(as_, "against", "away", 1.2)) / 2.0
    lam_a = (goals_avg(as_, "for",    "away", 1.0) + goals_avg(hs,  "against", "home", 1.3)) / 2.0
    lam_h = max(lam_h, 0.4)
    lam_a = max(lam_a, 0.4)

    total_a = form_a + inj_a + h2h_a
    hp2 = max(0.05, min(hp + total_a, 0.90))
    ap2 = max(0.05, min(ap - total_a*0.5, 0.90))
    dp2 = max(0.05, 1.0 - hp2 - ap2)
    nv  = no_vig([hp2, ap2, dp2])

    hlu = lu_info(lineups, hid)
    alu = lu_info(lineups, aid)

    factors = []
    if pred:          factors.append("prediccion_6_algoritmos")
    if injuries:      factors.append("lesiones")
    if h2h:           factors.append("h2h")
    if hs or as_:     factors.append("estadisticas")
    if hform != 0.5:  factors.append("forma")
    if hlu["ok"]:     factors.append("alineacion")

    return {
        "home_p":    round(nv[0], 4),
        "away_p":    round(nv[1], 4),
        "draw_p":    round(nv[2], 4),
        "btts_p":    poisson_btts(lam_h, lam_a),
        "over25_p":  poisson_over(lam_h, lam_a, 2.5),
        "under25_p": round(1-poisson_over(lam_h, lam_a, 2.5), 4),
        "over15_p":  poisson_over(lam_h, lam_a, 1.5),
        "under15_p": round(1-poisson_over(lam_h, lam_a, 1.5), 4),
        "over35_p":  poisson_over(lam_h, lam_a, 3.5),
        "under35_p": round(1-poisson_over(lam_h, lam_a, 3.5), 4),
        "lam_h":     round(lam_h, 2),
        "lam_a":     round(lam_a, 2),
        "h_inj":     int(injury_pen(injuries, hid)/0.03),
        "a_inj":     int(injury_pen(injuries, aid)/0.03),
        "h_form":    hlu["formation"],
        "a_form":    alu["formation"],
        "lu_ok":     hlu["ok"] and alu["ok"],
        "confidence":min(50+len(factors)*8, 95),
        "factors":   factors,
    }

# -- GROQ AI -------------------------------------------------------------------

GROQ_SYSTEM = """Eres un analista experto en pronosticos deportivos con 20 anos de experiencia en futbol, basketball y baseball.
Tu rol es complementar el analisis estadistico con contexto cualitativo: motivacion, momento de forma, factores psicologicos, lesiones clave.
IMPORTANTE: Tu analisis es UN FACTOR MAS (peso 35%) dentro de un modelo estadistico (65%).
Responde SOLO con JSON valido, sin texto adicional:
{"prob_home":0.XX,"prob_away":0.XX,"prob_draw":0.XX,"confianza":0.XX,"recomendacion":"HOME|AWAY|DRAW|NO_BET","razonamiento":"max 2 oraciones","factores":["f1","f2","f3"]}
Las probabilidades deben sumar 1.0. confianza entre 0 y 1."""

def groq_analyze(home, away, league, sport, stats_p, h_inj, a_inj, api_advice):
    key = get_key("GROQ_API_KEY")
    if not key: return {}
    try:
        msg = f"""Partido: {away} vs {home} | {league} | {sport}
Prob estadistica: local {round(stats_p.get('home_p',0.45)*100,1)}% | empate {round(stats_p.get('draw_p',0.25)*100,1)}% | visitante {round(stats_p.get('away_p',0.30)*100,1)}%
Goles esperados: local {stats_p.get('lam_h',1.4)} | visitante {stats_p.get('lam_a',1.0)}
Lesiones confirmadas: local {h_inj} | visitante {a_inj}
Consejo API: {api_advice or 'no disponible'}
Analiza y complementa. Devuelve SOLO el JSON."""

        r = requests.post(GROQ_BASE,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": GROQ_MODEL, "messages": [
                {"role": "system", "content": GROQ_SYSTEM},
                {"role": "user",   "content": msg}
            ], "max_tokens": 250, "temperature": 0.25},
            timeout=6)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"].strip()
        content = content.replace("```json","").replace("```","").strip()
        res = json.loads(content)

        # Normalize
        t = res.get("prob_home",0) + res.get("prob_away",0) + res.get("prob_draw",0)
        if t > 0:
            res["prob_home"] = round(res.get("prob_home",0)/t, 4)
            res["prob_away"] = round(res.get("prob_away",0)/t, 4)
            res["prob_draw"] = round(res.get("prob_draw",0)/t, 4)
        return res
    except Exception as e:
        print(f"Groq: {e}")
        return {}

def blend(stats, groq, w=0.35):
    if not groq: return stats
    sw = 1.0 - w
    hp = stats["home_p"]*sw + groq.get("prob_home", stats["home_p"])*w
    ap = stats["away_p"]*sw + groq.get("prob_away", stats["away_p"])*w
    orig_dp = stats.get("draw_p")
    if orig_dp is not None:
        dp = orig_dp*sw + groq.get("prob_draw", orig_dp)*w
        t  = hp+ap+dp
        new_dp = round(dp/t, 4)
    else:
        t  = hp+ap
        new_dp = None
    r  = dict(stats)
    r["home_p"]       = round(hp/t, 4)
    r["away_p"]       = round(ap/t, 4)
    r["draw_p"]       = new_dp
    r["groq_ok"]      = True
    r["groq_conf"]    = groq.get("confianza", 0)
    r["groq_reason"]  = groq.get("razonamiento", "")
    r["groq_factors"] = groq.get("factores", [])
    return r

# -- PROCESS SINGLE FIXTURE ----------------------------------------------------

def process_fixture(fix_data, season):
    """Process one fixture: fetch all data sequentially and compute probs."""
    fix    = fix_data.get("fixture") or {}
    teams  = fix_data.get("teams") or {}
    league = fix_data.get("league") or {}
    home_t = teams.get("home") or {}
    away_t = teams.get("away") or {}
    home   = home_t.get("name") or ""
    away   = away_t.get("name") or ""
    if not home or not away: return None

    fid  = fix.get("id")
    hid  = home_t.get("id")
    aid  = away_t.get("id")
    lid  = league.get("id")
    start= fix.get("date") or ""

    # Sequential fetch (avoids nested ThreadPoolExecutor deadlock)
    pred     = fetch_prediction(fid)
    injuries = fetch_injuries(fid)
    h2h      = fetch_h2h(hid, aid) if hid and aid else []
    hs       = fetch_team_stats(hid, lid, season) if hid and lid else {}
    as_      = fetch_team_stats(aid, lid, season) if aid and lid else {}
    lineups  = fetch_lineups(fid)

    probs = calculate_probs(fix_data, pred, injuries, h2h, hs, as_, lineups)

    # Groq analysis
    api_adv = (pred.get("predictions") or {}).get("advice") or ""
    api_win = ((pred.get("predictions") or {}).get("winner") or {}).get("name") or ""
    groq_r  = groq_analyze(home, away, league.get("name",""), "soccer",
                           probs, probs["h_inj"], probs["a_inj"], api_adv)
    probs   = blend(probs, groq_r)

    return {
        "id":        str(fid),
        "home":      home, "away": away,
        "sport":     "soccer",
        "league":    league.get("name") or "",
        "start":     start,
        "home_p":    probs["home_p"], "away_p": probs["away_p"],
        "draw_p":    probs["draw_p"],
        "btts_p":    probs["btts_p"],
        "over25_p":  probs["over25_p"], "under25_p": probs["under25_p"],
        "over15_p":  probs["over15_p"], "under15_p": probs["under15_p"],
        "over35_p":  probs["over35_p"], "under35_p": probs["under35_p"],
        "h_inj":     probs["h_inj"],    "a_inj": probs["a_inj"],
        "h_form":    probs["h_form"],   "a_form": probs["a_form"],
        "lu_ok":     probs["lu_ok"],
        "confidence":probs["confidence"],
        "factors":   probs["factors"],
        "api_advice":api_adv, "api_winner": api_win,
        "groq_ok":   probs.get("groq_ok", False),
        "groq_conf": probs.get("groq_conf", 0),
        "groq_reason":probs.get("groq_reason",""),
        "groq_factors":probs.get("groq_factors",[]),
        "source":    "API-Football",
    }

# -- ESPN ----------------------------------------------------------------------

ESPN_LEAGUES = [
    ("basketball","nba","NBA"),
    ("baseball","mlb","MLB"),
    ("soccer","eng.1","Premier League"),
    ("soccer","esp.1","La Liga"),
    ("soccer","ger.1","Bundesliga"),
    ("soccer","ita.1","Serie A"),
    ("soccer","fra.1","Ligue 1"),
    ("soccer","usa.1","MLS"),
    ("soccer","arg.1","Liga Argentina"),
    ("soccer","bra.1","Brasileirao"),
    ("soccer","uefa.champions","Champions League"),
    ("soccer","uefa.europa","Europa League"),
    ("soccer","mex.1","Liga MX"),
    ("soccer","ned.1","Eredivisie"),
    ("soccer","por.1","Primeira Liga"),
]

def fetch_espn(target_date):
    events = []
    date_fmt = target_date.replace("-","")  # YYYYMMDD

    def fetch_league(sport, slug, name):
        url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{slug}/scoreboard"
        data = get_json(url, params={"dates": date_fmt})
        result = []
        for ev in data.get("events",[]):
            comp   = (ev.get("competitions") or [{}])[0]
            status = comp.get("status",{}).get("type",{}).get("name","")
            if status in ("STATUS_FINAL","STATUS_IN_PROGRESS"): continue
            comps  = comp.get("competitors",[])
            home   = next((c for c in comps if c.get("homeAway")=="home"),{})
            away   = next((c for c in comps if c.get("homeAway")=="away"),{})
            hn     = (home.get("team") or {}).get("displayName") or ""
            an     = (away.get("team") or {}).get("displayName") or ""
            if not hn or not an: continue
            start  = ev.get("date") or ""
            if not is_valid_fixture(start, target_date): continue

            def rec(c):
                recs = c.get("records") or []
                if recs:
                    p = (recs[0].get("summary") or "0-0").split("-")
                    return int(p[0]) if p else 0, int(p[1]) if len(p)>1 else 0
                return 0, 0

            hw,hl = rec(home)
            aw,al = rec(away)
            is_soc = sport=="soccer"
            hp_r = hw/max(hw+hl,1)
            ap_r = aw/max(aw+al,1)
            if is_soc:
                dp=0.28; t=hp_r+ap_r+dp
                hp,ap,dp = hp_r/t, ap_r/t, dp/t
            else:
                t=hp_r+ap_r or 1; hp,ap,dp = hp_r/t, ap_r/t, None

            lh=1.4 if is_soc else None
            la=1.0 if is_soc else None
            result.append({
                "id": ev.get("id",""), "home":hn,"away":an,
                "sport":"soccer" if is_soc else sport,
                "league":name,"start":start,
                "home_p":round(hp,4),"away_p":round(ap,4),
                "draw_p":round(dp,4) if dp else None,
                "btts_p":  round(poisson_btts(lh,la),4) if lh else None,
                "over25_p":round(poisson_over(lh,la,2.5),4) if lh else None,
                "under25_p":round(1-poisson_over(lh,la,2.5),4) if lh else None,
                "over15_p":round(poisson_over(lh,la,1.5),4) if lh else None,
                "under15_p":round(1-poisson_over(lh,la,1.5),4) if lh else None,
                "over35_p":round(poisson_over(lh,la,3.5),4) if lh else None,
                "under35_p":round(1-poisson_over(lh,la,3.5),4) if lh else None,
                "h_inj":0,"a_inj":0,"h_form":"","a_form":"","lu_ok":False,
                "confidence":50,"factors":["record"],
                "api_advice":"","api_winner":"",
                "groq_ok":False,"groq_conf":0,"groq_reason":"","groq_factors":[],
                "source":"ESPN",
            })
        return result

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(fetch_league,s,sl,n): n for s,sl,n in ESPN_LEAGUES}
        for f in as_completed(futures, timeout=10):
            try: events.extend(f.result())
            except: pass

    return events

# -- ODDS API ------------------------------------------------------------------

SPORT_KEYS = [
    "basketball_nba","baseball_mlb","soccer_epl","soccer_spain_la_liga",
    "soccer_germany_bundesliga","soccer_italy_serie_a","soccer_france_ligue_one",
    "soccer_uefa_champs_league","soccer_uefa_europa_league","soccer_usa_mls",
    "soccer_brazil_campeonato","soccer_argentina_primera_division",
    "soccer_netherlands_eredivisie","soccer_portugal_primeira_liga","soccer_mexico_ligamx",
]

def fetch_odds():
    key = get_key("ODDS_API_KEY")
    if not key: return []
    all_games = {}
    def fetch_sport(sk):
        mkt = "h2h,btts,totals,double_chance" if "soccer" in sk else "h2h,totals"
        data = get_json(f"{ODDS_BASE}/sports/{sk}/odds",
                        params={"apiKey":key,"regions":"eu,uk,us","markets":mkt,"oddsFormat":"decimal"})
        return data if isinstance(data, list) else []

    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(fetch_sport,sk): sk for sk in SPORT_KEYS}
        for f in as_completed(futures, timeout=12):
            try:
                for g in f.result():
                    all_games[g.get("id","")] = g
            except: pass

    return list(all_games.values())

def best_odds(bks, mkt_key, outcome):
    best = 1.01
    for bk in bks:
        for m in bk.get("markets",[]):
            if m["key"] == mkt_key:
                for out in m.get("outcomes",[]):
                    if outcome.lower() in (out.get("name") or "").lower():
                        best = max(best, out["price"])
    return best

def match_odds(event, odds_list):
    hn = event["home"].lower()
    an = event["away"].lower()
    for g in odds_list:
        gh = (g.get("home_team") or "").lower()
        ga = (g.get("away_team") or "").lower()
        hm = any(w in gh or gh in w for w in hn.split() if len(w)>3)
        am = any(w in ga or ga in w for w in an.split() if len(w)>3)
        if hm or am: return g
    return None

# -- MARKET SIGNALS ------------------------------------------------------------

def build_signals(event, odds_game):
    """Analyze every possible market per match. Use real odds when available."""
    bks = odds_game.get("bookmakers",[]) if odds_game else []
    has_real = len(bks) > 0
    sigs = []

    hp = event["home_p"]; ap = event["away_p"]; dp = event.get("draw_p")
    hn = event["home"];   an = event["away"]

    def real_odd(mkt_key, outcome_name, line=None):
        best = 1.01
        for bk in bks:
            for mkt in bk.get("markets",[]):
                if mkt["key"] != mkt_key: continue
                for out in mkt.get("outcomes",[]):
                    nm = (out.get("name") or "").lower()
                    pt = out.get("point") or 0
                    if outcome_name.lower() in nm:
                        if line is None or abs(pt - line) < 0.01 or pt == 0:
                            best = max(best, out.get("price", 1.01))
        return best if best > 1.01 else None

    def est(prob, margin=0.05):
        # Estimated fair odds with slight positive bias
        # Real bookmaker adds ~8% margin, we give ourselves ~5% edge
        return round(1.0 / max(prob, 0.01) * (1.0 + margin), 2)

    def add(label, mkt, outcome, prob, odds, mtype):
        if not prob or prob <= 0 or odds < 1.20: return
        ev = calc_ev(prob, odds)
        sigs.append({
            "label": label, "market": mkt, "outcome": outcome,
            "market_type": mtype,
            "true_prob": round(prob * 100, 1),
            "odds": round(odds, 2),
            "ev_pct": round(ev * 100, 1),
            "kelly_pct": calc_kelly(prob, odds),
            "real_odds": has_real,
            "_p": prob, "_o": odds,
        })

    # 1X2
    add(hn+" GANA","h2h",hn,hp, real_odd("h2h",hn)       or est(hp),  "1X2")
    add(an+" GANA","h2h",an,ap, real_odd("h2h",an)       or est(ap),  "1X2")
    if dp is not None:
        add("EMPATE","h2h","Draw",dp, real_odd("h2h","draw") or est(dp), "1X2")
        p1x=hp+dp; px2=ap+dp; p12=hp+ap
        add(hn+" o Empate (1X)","double_chance","1X",p1x, real_odd("double_chance","1X") or est(p1x), "Doble Oportunidad")
        add(an+" o Empate (X2)","double_chance","X2",px2, real_odd("double_chance","X2") or est(px2), "Doble Oportunidad")
        add(hn+" o "+an+" (12)","double_chance","12",p12, real_odd("double_chance","12") or est(p12), "Doble Oportunidad")

    # BTTS
    bp = event.get("btts_p")
    if bp:
        add("AMBOS MARCAN - Si","btts","Yes",bp,   real_odd("btts","yes") or est(bp),   "BTTS")
        add("AMBOS MARCAN - No","btts","No", 1-bp, real_odd("btts","no")  or est(1-bp), "BTTS")

    # Over/Under per line
    for line,ok,uk,ol,ul in [
        (2.5,"over25_p","under25_p","MAS DE 2.5 GOLES","MENOS DE 2.5 GOLES"),
        (1.5,"over15_p","under15_p","MAS DE 1.5 GOLES","MENOS DE 1.5 GOLES"),
        (3.5,"over35_p","under35_p","MAS DE 3.5 GOLES","MENOS DE 3.5 GOLES"),
    ]:
        op = event.get(ok); up = event.get(uk)
        if not op: continue
        oo = real_odd("totals","over",line)  or est(op)
        uo = real_odd("totals","under",line) or (est(up) if up else None)
        add(ol,"totals",f"Over {line}", op, oo, "Over/Under")
        if up and uo: add(ul,"totals",f"Under {line}",up,uo,"Over/Under")

    return sigs


def score_signal(s):
    """
    Score a signal combining EV, probability, odds and confidence.
    High EV alone is not enough - we also want high probability and fair odds.
    """
    ev    = s["ev_pct"]
    prob  = s["_p"] * 100
    odds  = s["_o"]
    real  = 1.2 if s.get("real_odds") else 0.7  # bonus for real bookmaker odds

    # Penalize extreme odds (too risky) and very low odds (bad value)
    odds_score = 1.0
    if odds < 1.50:   odds_score = 0.5   # too low
    elif odds < 1.80: odds_score = 0.85
    elif odds < 2.50: odds_score = 1.0   # sweet spot
    elif odds < 4.00: odds_score = 0.85
    else:             odds_score = 0.6   # too risky

    # Penalize low probability even if EV is good
    prob_score = 1.0
    if prob >= 70:    prob_score = 1.2   # high confidence
    elif prob >= 60:  prob_score = 1.0
    elif prob >= 52:  prob_score = 0.8
    else:             prob_score = 0.5

    return ev * odds_score * prob_score * real

def score_signal(s):
    """Score combining EV, probability, odds quality, real odds bonus."""
    ev     = s["ev_pct"]
    prob   = s["_p"]
    odds   = s["_o"]
    real_w = 1.3 if s.get("real_odds") else 0.85

    if odds < 1.40:    ow = 0.4
    elif odds < 1.60:  ow = 0.75
    elif odds < 2.00:  ow = 1.0
    elif odds < 3.00:  ow = 1.1
    elif odds < 5.00:  ow = 0.85
    else:              ow = 0.5

    if prob >= 0.72:   pw = 1.3
    elif prob >= 0.62: pw = 1.1
    elif prob >= 0.52: pw = 0.9
    else:              pw = 0.65

    return ev * ow * pw * real_w

def generate_recommendation(sigs, event):
    """Always return the best pick. Scores every signal and picks the top one."""
    if not sigs:
        return None
    for s in sigs:
        s["score"] = score_signal(s)
    ranked = sorted(sigs, key=lambda x: x["score"], reverse=True)
    best   = ranked[0]
    # Skip only if absolutely no value
    if best["score"] <= 0 and best["ev_pct"] < -20:
        return None
    prob = best["_p"] * 100
    ev   = best["ev_pct"]
    if ev >= 12 and prob >= 65:
        nivel = "MUY ALTA"; emoji_nivel = "FUEGO"
    elif ev >= 5 and prob >= 55:
        nivel = "ALTA";     emoji_nivel = "OK"
    elif ev >= 0 and prob >= 48:
        nivel = "MEDIA";    emoji_nivel = "RAYO"
    else:
        nivel = "BAJA";     emoji_nivel = "ALERTA"
    reasons = []
    if prob >= 70:             reasons.append(f"Alta probabilidad ({prob:.0f}%)")
    if ev >= 8:                reasons.append(f"Buen valor esperado (+{ev:.1f}%)")
    if best["_o"] >= 1.80:    reasons.append(f"Cuota atractiva ({best['_o']})")
    if best.get("real_odds"): reasons.append("Cuota verificada en bookmakers")
    if not reasons:           reasons.append(f"Mejor apuesta disponible ({prob:.0f}% prob)")
    best["nivel_confianza"] = nivel
    best["emoji_nivel"]     = emoji_nivel
    best["razon_pick"]      = " - ".join(reasons)
    best["todas_opciones"]  = [
        {"label":s["label"],"market_type":s["market_type"],
         "prob":s["true_prob"],"odds":s["odds"],
         "ev_pct":s["ev_pct"],"score":round(s["score"],2)}
        for s in ranked[:6]
    ]
    return best


def best_signal(sigs):
    """Legacy wrapper - returns best signal by EV for compatibility."""
    valid = [s for s in sigs if s["_p"]>=MIN_PROB and s["_o"]>=MIN_ODDS and s["ev_pct"]>0]
    return max(valid, key=lambda x: x["ev_pct"]) if valid else None

# -- COMBINADAS ----------------------------------------------------------------

def build_combinadas(picks):
    pool = list({p["partido"]:p for p in picks}.values())
    if len(pool)<2: return []
    combos = []
    for r in range(2, min(5,len(pool)+1)):
        for combo in itertools.combinations(pool,r):
            c_odds = round(reduce(lambda a,b:a*b,[c["odds"] for c in combo]),2)
            c_prob = reduce(lambda a,b:a*b,[c["_p"] for c in combo])
            if c_odds<MIN_ODDS or c_prob<0.25: continue
            c_ev = round((c_prob*c_odds)-1.0,4)
            if c_ev<=0: continue
            avg_conf = sum(c.get("confidence",50) for c in combo)/len(combo)
            combos.append({
                "picks":[c["label"]+" ("+c["partido"]+")" for c in combo],
                "partidos":[c["partido"] for c in combo],
                "deportes":list({c["sport"] for c in combo}),
                "cuota_total":c_odds,
                "prob_total":round(c_prob*100,1),
                "ev_pct":round(c_ev*100,1),
                "kelly_pct":round(max(0,min(c_prob-(1-c_prob)/max(c_odds-1,.01),.05)*.15*100,0),2),
                "legs":r,
                "avg_confidence":round(avg_conf),
            })
    combos.sort(key=lambda x:x["ev_pct"]*x["avg_confidence"]/100,reverse=True)
    return combos[:8]

def dedup(events):
    seen={}
    pri={"API-Football":4,"ESPN":2,"OddsAPI":1}
    for ev in events:
        k=ev["home"].lower()[:6]+"_"+ev["away"].lower()[:6]
        if k not in seen or pri.get(ev.get("source",""),0)>pri.get(seen[k].get("source",""),0):
            seen[k]=ev
    return list(seen.values())

# -- MAIN ENDPOINT -------------------------------------------------------------

@app.route("/api/signals", methods=["GET"])
def get_signals():
    # Date param
    target_date = request.args.get("date","")
    if not target_date:
        target_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try: datetime.strptime(target_date,"%Y-%m-%d")
    except: target_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    now    = datetime.now(timezone.utc)
    season = (now.year-1) if now.month<7 else now.year

    all_events   = []
    sources_used = []

    # Run ESPN + Odds + API-Football fixtures IN PARALLEL for speed
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_espn     = ex.submit(fetch_espn, target_date)
        f_odds     = ex.submit(fetch_odds)
        f_fixtures = ex.submit(fetch_fixtures, target_date)

        try: espn = f_espn.result(timeout=12)
        except: espn = []
        try: odds_list = f_odds.result(timeout=12)
        except: odds_list = []
        try: fixtures = f_fixtures.result(timeout=10)
        except: fixtures = []

    if espn: 
        all_events.extend(espn)
        sources_used.append(f"ESPN ({len(espn)} eventos)")
    if odds_list:
        sources_used.append(f"The Odds API ({len(odds_list)} juegos)")

    # API-Football deep analysis - max 3 fixtures, sequential, strict time
    if fixtures:
        sources_used.append(f"API-Football ({len(fixtures)} partidos)")
        for fix in fixtures[:3]:
            try:
                ev = process_fixture(fix, season)
                if ev: all_events.append(ev)
            except Exception as e:
                print(f"Fixture error: {e}")

    # Fallback from odds if no events
    if not all_events:
        for g in odds_list:
            start = g.get("commence_time","")
            if not is_valid_fixture(start, target_date): continue
            all_events.append({
                "id":g.get("id",""),"home":g.get("home_team",""),"away":g.get("away_team",""),
                "sport":g.get("sport_key",""),"league":g.get("sport_title",""),"start":start,
                "home_p":0.50,"away_p":0.50,"draw_p":None,
                "btts_p":None,"over25_p":None,"under25_p":None,
                "over15_p":None,"under15_p":None,"over35_p":None,"under35_p":None,
                "h_inj":0,"a_inj":0,"h_form":"","a_form":"","lu_ok":False,
                "confidence":40,"factors":[],"api_advice":"","api_winner":"",
                "groq_ok":False,"groq_conf":0,"groq_reason":"","groq_factors":[],"source":"OddsAPI",
            })

    # Build picks
    picks = []
    for event in all_events:
        og    = match_odds(event, odds_list)
        sigs  = build_signals(event, og)
        best  = generate_recommendation(sigs, event)
        if not best: continue
        bks   = [b["title"] for b in (og.get("bookmakers",[]) if og else [])[:5]]
        best.update({
            "partido":    event["away"]+" vs "+event["home"],
            "home":       event["home"], "away": event["away"],
            "sport":      event["sport"], "league": event["league"],
            "start":      fmt_date(event["start"]),
            "emoji":      sport_icon(event["sport"]),
            "bookmakers": bks,
            "source":     event.get("source",""),
            "confidence": event.get("confidence",50),
            "factors":    event.get("factors",[]),
            "api_advice": event.get("api_advice",""),
            "api_winner": event.get("api_winner",""),
            "h_inj":      event.get("h_inj",0),
            "a_inj":      event.get("a_inj",0),
            "groq_ok":    event.get("groq_ok",False),
            "groq_conf":  event.get("groq_conf",0),
            "groq_reason":event.get("groq_reason",""),
            "groq_factors":   event.get("groq_factors",[]),
            "nivel_confianza":best.get("nivel_confianza","MEDIA"),
            "emoji_nivel":    best.get("emoji_nivel","RAYO"),
            "razon_pick":     best.get("razon_pick",""),
            "todas_opciones": best.get("todas_opciones",[]),
            "_p": best["_p"], "_o": best["_o"],
        })
        picks.append(best)

    picks.sort(key=lambda x: x["ev_pct"]*x.get("confidence",50)/100, reverse=True)
    for i,p in enumerate(picks): p["numero"]=i+1

    combinadas = build_combinadas(picks) if len(picks)>=2 else []

    for p in picks:
        p.pop("_p",None); p.pop("_o",None)

    return jsonify({
        "success":         True,
        "date_analyzed":   target_date,
        "timestamp":       datetime.now(timezone.utc).isoformat(),
        "total_events":    len(all_events),
        "total_picks":     len(picks),
        "total_combinadas":len(combinadas),
        "picks":           picks,
        "combinadas":      combinadas,
        "sources_used":    sources_used,
        "filters":         {"min_prob_pct":MIN_PROB*100,"min_odds":MIN_ODDS},
    })

@app.route("/api/debug", methods=["GET"])
def debug():
    target = request.args.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    
    # Test API-Football directly
    af_test = get_json(AF_BASE+"/fixtures", af_headers(), 
                       {"date": target, "status": "NS"})
    af_errors = af_test.get("errors") or {}
    af_count  = len(af_test.get("response") or [])
    af_remaining = af_test.get("results", 0)
    
    fixtures = fetch_fixtures(target)
    espn     = fetch_espn(target)
    
    keys_ok = {
        "APIFOOTBALL_KEY": bool(get_key("APIFOOTBALL_KEY")),
        "ODDS_API_KEY":    bool(get_key("ODDS_API_KEY")),
        "GROQ_API_KEY":    bool(get_key("GROQ_API_KEY")),
    }
    # Test odds API
    odds_test = fetch_odds()
    
    # Test signal generation on first ESPN event
    sig_test = []
    if espn:
        test_ev = espn[0]
        test_og = match_odds(test_ev, odds_test)
        test_sigs = build_signals(test_ev, test_og)
        rec = generate_recommendation(test_sigs, test_ev)
        sig_test = [{
            "match": test_ev["home"]+" vs "+test_ev["away"],
            "odds_match_found": test_og is not None,
            "signals_generated": len(test_sigs),
            "best_pick": rec["label"] if rec else None,
            "best_ev": rec["ev_pct"] if rec else None,
            "all_signals": [{"label":s["label"],"prob":s["true_prob"],"odds":s["odds"],"ev":s["ev_pct"]} for s in test_sigs[:6]],
        }]
    
    return jsonify({
        "date":              target,
        "keys_loaded":       keys_ok,
        "api_football_raw":  {"count": af_count, "errors": af_errors},
        "football_filtered": len(fixtures),
        "espn_count":        len(espn),
        "odds_count":        len(odds_test),
        "signal_test":       sig_test,
        "espn_sample":       [{"home":e["home"],"away":e["away"],"league":e["league"],"start":e["start"]} for e in espn[:5]],
        "football_sample":   [{"home":(f.get("teams") or {}).get("home",{}).get("name"),
                               "away":(f.get("teams") or {}).get("away",{}).get("name"),
                               "start":(f.get("fixture") or {}).get("date")} 
                              for f in fixtures[:3]],
    })

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status":"ok","ts":datetime.now(timezone.utc).isoformat()})

if __name__ == "__main__":
    app.run(debug=True, port=5000)

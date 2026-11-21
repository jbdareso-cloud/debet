"""DeBet - personal AI-checked football betting desk (single user). Run with:  streamlit run app.py

The old multi-user/subscriber version is kept in archive/app_multiuser.py.
"""

import html
import json
import math
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components


def _load_cloud_secrets():
    """On Streamlit Cloud the keys live in the app's Secrets; copy them into the environment where the
    rest of DeBet reads them. Locally there's no secrets file and .env is used instead."""
    import os
    try:
        for key in ("OPENROUTER_API_KEY", "DATABASE_URL"):
            if key in st.secrets and not os.environ.get(key):
                os.environ[key] = str(st.secrets[key])
    except Exception:
        pass


_load_cloud_secrets()

import database as db  # noqa: E402  (must come after the secrets are loaded)
import analysis_jobs as ajobs
import result_verifier as rv
import tipster_site as ts
import value_engine as ve
import client_tiers as ct
from debate_engine import format_cost, parse_tip

st.set_page_config(page_title="DeBet", page_icon="⚽", layout="wide", initial_sidebar_state="collapsed")

# ---------------------------------------------------------------------------
# Look & feel
# ---------------------------------------------------------------------------
THEME_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700;800&family=Inter:wght@400;500;600;700&display=swap');
:root {--db-bg:#0B1220; --db-surface:#111A2B; --db-line:#24324A; --db-text:#E5EAF1; --db-muted:#8D9BB1;
       --db-pitch:#10B981; --db-pitch-hi:#34D399;}
html, body, .stApp, [data-testid="stMarkdownContainer"] {font-family:'Inter',system-ui,sans-serif;}
.stApp {background: radial-gradient(1100px 480px at 8% -8%, rgba(16,185,129,.10), transparent 60%), var(--db-bg);}
[data-testid="stHeader"] {background: transparent;}
.block-container, [data-testid="stMainBlockContainer"] {max-width: 1250px; padding-top: 1.5rem;}
h1, h2, h3, h4 {font-family:'Barlow Condensed','Inter',sans-serif !important; letter-spacing:.3px;}
[data-testid="stMetric"] {background:var(--db-surface); border:1px solid var(--db-line);
  border-top:3px solid var(--db-pitch); border-radius:10px; padding:.7rem .9rem;}
[data-testid="stMetricLabel"] p {text-transform:uppercase; letter-spacing:.5px; font-size:.74rem;
  color:var(--db-muted); font-weight:600;}
[data-testid="stMetricValue"] {font-family:'Barlow Condensed',sans-serif; font-weight:700;}
.stTabs [data-baseweb="tab"] {font-weight:600; font-size:1rem;}
.db-brand {font-family:'Barlow Condensed',sans-serif; font-weight:800; font-size:2.1rem; line-height:1;
  letter-spacing:1px; color:var(--db-text);}
.db-brand span {color:var(--db-pitch-hi);}
.db-kicker {font-size:.72rem; font-weight:700; letter-spacing:2.5px; color:var(--db-pitch-hi);
  text-transform:uppercase; margin:.3rem 0 1rem;}
.element-container:has(iframe[height="0"]), [data-testid="stElementContainer"]:has(iframe[height="0"]) {display:none;}
@media (max-width: 768px) {
  .block-container, [data-testid="stMainBlockContainer"] {padding:1rem .6rem 2rem !important;}
  /* keep the logo and the settings button on one row */
  .st-key-topbar [data-testid="stHorizontalBlock"] {flex-wrap:nowrap !important; flex-direction:row !important;}
  .st-key-topbar [data-testid="stColumn"]:first-child {flex:1 1 auto !important; min-width:0 !important;}
  .st-key-topbar [data-testid="stColumn"]:last-child {flex:0 0 64px !important; min-width:64px !important;}
  [data-testid="stMetricLabel"] p {white-space:normal !important;}
  .stButton button, [data-testid="stFormSubmitButton"] button {width:100% !important; min-height:44px;}
  input, textarea, [data-baseweb="select"] {font-size:16px !important;}
  [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] [data-testid="stMetric"]) {
    flex-wrap:wrap !important; flex-direction:row !important; gap:.5rem !important;}
  [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] [data-testid="stMetric"]) > [data-testid="stColumn"] {
    flex:1 1 calc(50% - .5rem) !important; min-width:calc(50% - .5rem) !important;}
  .stTabs [data-baseweb="tab"] {font-size:.85rem; padding:0 .5rem;}
}
</style>
"""

# Phone "Add to Home Screen" meta tags (Streamlit can't write to <head> directly).
MOBILE_META_JS = """<script>
(function () {
  const head = window.parent && window.parent.document && window.parent.document.head;
  if (!head) return;
  const tags = {"theme-color": "#147A2E", "apple-mobile-web-app-capable": "yes", "mobile-web-app-capable": "yes",
                "apple-mobile-web-app-status-bar-style": "black-translucent", "apple-mobile-web-app-title": "DeBet",
                "application-name": "DeBet", "format-detection": "telephone=no"};
  for (const [name, content] of Object.entries(tags)) {
    let el = head.querySelector('meta[name="' + name + '"]');
    if (!el) { el = window.parent.document.createElement("meta"); el.name = name; head.appendChild(el); }
    el.content = content;
  }
  const vp = head.querySelector('meta[name="viewport"]');
  if (vp) vp.content = "width=device-width, initial-scale=1, viewport-fit=cover";
})();
</script>"""

RESULT_BADGE = {"WON": "✅ Won", "LOST": "❌ Lost", "VOID": "⚪ Void", "PENDING": "⏳ Pending"}
VERDICT_ICON = {"PLAY AS IS": "🟢 Play", "PLAY REDUCED STAKE": "🟠 Reduced stake", "AVOID": "🔴 Avoid"}
DECISION_BADGE = {"BEST": "⭐ Best pick", "VALUE": "🟢 Value", "NO VALUE": "⚪ No value",
                  "SPLIT": "🟠 Models disagree", "SHORT PRICE": "⏭️ Price too short", "NO ODDS": "❔ No odds"}
EAT = timezone(timedelta(hours=3))


# ---------------------------------------------------------------------------
# Startup (once per server process)
# ---------------------------------------------------------------------------
@st.cache_resource
def bootstrap():
    db.init_db()  # first start only: prints a generated admin password to the server console
    # No automatic result check: results are entered by hand (or with the paid button in Results).
    return {"startup_verification": None}


BOOT = bootstrap()


# ---------------------------------------------------------------------------
# Login (only you - the admin account). Stays logged in for 30 days via a cookie.
# ---------------------------------------------------------------------------
SESSION_COOKIE = "debet_session"


def _cookie_js(token):
    max_age = db.SESSION_DAYS * 86400 if token else 0
    return ("<script>(function () { const p = window.parent;"
            " const secure = p.location.protocol === 'https:' ? '; Secure' : '';"
            f" p.document.cookie = '{SESSION_COOKIE}={token or ''}; Max-Age={max_age}; Path=/; SameSite=Lax' + secure;"
            " })();</script>")


def inject_page_scripts():
    cookie = ""
    if st.session_state.get("session_token"):
        cookie = _cookie_js(st.session_state["session_token"])
    elif st.session_state.get("logged_out"):
        cookie = _cookie_js(None)
    components.html(MOBILE_META_JS + cookie, height=0)


def start_session(user_id):
    st.session_state["user_id"] = user_id
    st.session_state["session_token"] = db.create_session(user_id)
    st.session_state.pop("logged_out", None)


def logout(rerun=True):
    db.delete_session(st.session_state.get("session_token"))
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    st.session_state["logged_out"] = True
    if rerun:
        st.rerun()


def current_user():
    """The logged-in user (admin = you, subscriber = a client), or None."""
    uid = st.session_state.get("user_id")
    if not uid and not st.session_state.get("logged_out"):
        token = st.context.cookies.get(SESSION_COOKIE)
        user = db.get_session_user(token)
        if user:
            st.session_state["user_id"], st.session_state["session_token"] = user["id"], token
            uid = user["id"]
    if not uid:
        return None
    user = db.get_user(uid)
    token = st.session_state.get("session_token")
    if not user or user["role"] not in ("admin", "subscriber") or (token and not db.get_session_user(token)):
        logout(rerun=False)
        return None
    return user


def login_page():
    st.markdown('<div class="db-brand">DE<span>BET</span></div>'
                '<div class="db-kicker">Daily football tips</div>', unsafe_allow_html=True)
    _, mid, _ = st.columns([1, 2, 1])
    with mid:
        t_join, t_login = st.tabs(["Join free", "Log in"])
    with t_join, st.form("join"):
        phone = st.text_input("Phone number", placeholder="0772 123456")
        pw = st.text_input("Choose a password (6+ characters)", type="password")
        adult = st.checkbox("I'm 18+ and I understand tips are opinions, not guarantees.")
        if st.form_submit_button("Join", type="primary", width="stretch"):
            digits = db.normalize_phone(phone)
            if len(digits) < 9:
                st.error("Enter your phone number.")
            elif len(pw) < 6:
                st.error("The password needs at least 6 characters.")
            elif not adult:
                st.error("Please tick the 18+ box.")
            else:
                try:
                    user = db.register_user(f"Client {digits[-4:]}", phone, pw)
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    start_session(user["id"])
                    st.rerun()
    with t_login, st.form("login"):
        ident = st.text_input("Phone number", placeholder="0772 123456")
        pw = st.text_input("Password", type="password")
        if st.form_submit_button("Log in", type="primary", width="stretch"):
            locked = db.login_lock_minutes_left(ident)
            if locked:
                st.error(f"Too many wrong passwords. Try again in {locked} minute(s).")
                st.stop()
            user = db.login_user(ident, pw)
            if user and user["role"] in ("admin", "subscriber"):
                db.clear_login_failures(ident)
                start_session(user["id"])
                st.rerun()
            db.record_login_failure(ident)
            st.error("Wrong phone number or password.")
    st.caption("[Privacy](?page=privacy) · [Terms](?page=terms)")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def fmt_odds(odds):
    return f"{odds:.2f}" if isinstance(odds, (int, float)) else "—"


def fmt_money(value):
    if value is None:
        return "—"
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def fmt_signed(value):
    if value is None:
        return "—"
    return ("+" if value > 0 else "−" if value < 0 else "") + fmt_money(abs(value))


def fmt_date(value):
    try:
        return date.fromisoformat(str(value)[:10]).strftime("%a %d %b")
    except ValueError:
        return "No date"


def ai_label(tip):
    """'⭐ Best pick · 62% · +9%' for value-engine tips; the old verdict for older ones."""
    if tip.get("decision"):
        bits = [DECISION_BADGE.get(tip["decision"], tip["decision"])]
        if tip.get("p_final") is not None:
            bits.append(f"{tip['p_final']:.0%}")
        if tip.get("value") is not None:
            bits.append(f"{tip['value']:+.0%}")
        return " · ".join(bits)
    if not tip.get("ai_verdict"):
        return "⚠️ No verdict"
    score = tip.get("confidence_score")
    return f"{VERDICT_ICON.get(tip['ai_verdict'], tip['ai_verdict'])} · {score:g}/10" if score is not None \
        else VERDICT_ICON.get(tip["ai_verdict"], tip["ai_verdict"])


def source_label(tip):
    return db.SOURCE_LABELS.get(tip.get("tip_group"), "—")


def units(summary):
    return f"{summary['profit_units']:+.2f}u" if summary["settled"] else None


# ---------------------------------------------------------------------------
# 1. Dashboard
# ---------------------------------------------------------------------------
def dashboard(user):
    now = datetime.now(EAT)
    st.markdown(f"### {'Good morning' if now.hour < 12 else 'Good afternoon' if now.hour < 17 else 'Good evening'} "
                f"· {now.strftime('%A %d %B')}")

    lab = db.get_performance_lab()
    money = db.get_user_bankroll_stats(user["id"], 30)
    tipster, best = lab["group_a"], lab["ai_best"]
    c = st.columns(4)
    c[0].metric("Profit · 30d", fmt_signed(money["net_profit"]),
                f"ROI {money['roi']:+.1f}%" if money["roi"] is not None else None,
                help="From the bets you logged in My Bets (settled slips only).")
    c[1].metric("Daily tips", f"{tipster['win_rate']:.0f}%" if tipster["settled"] else "—",
                units(tipster), help="Every daily tip from the last 30 days, 1 unit each at the listed odds.")
    c[2].metric("AI best pick", f"{best['win_rate']:.0f}%" if best["settled"] else "—", units(best),
                help="Win rate of the one tip a day the AI picked as best value (or none), 1 unit each.")
    spend, calls = db.ai_spend(since_month_start=True)
    c[3].metric("AI spend · month", f"${spend:.2f}", f"{calls} runs" if calls else None, delta_color="off",
                help="Everything DeBet paid OpenRouter this calendar month: analyses, screenshot reading and "
                     "result checks. Set a spending limit on openrouter.ai as a safety net.")

    pending = [t for t in db.list_tips(result="PENDING") if t.get("match_date") is None
               or str(t["match_date"])[:10] >= (now.date() - timedelta(days=3)).isoformat()]
    bet_tips = {leg["tip_id"] for b in db.list_user_bets(user["id"]) if b["status"] == "PENDING"
                for leg in json.loads(b["legs"] or "[]")}
    st.markdown("#### 📅 Upcoming & waiting")
    if pending:
        pending.sort(key=lambda t: (str(t["match_date"] or "9999"), t.get("slot_number") or 99))
        st.dataframe(pd.DataFrame([{
            "Date": fmt_date(t["match_date"]), "Source": source_label(t), "Match": t["match_name"],
            "Tip": f"{t['user_proposed_tip']} @ {fmt_odds(t['odds'])}",
            "AI: decision · chance · value": ai_label(t),
            "Bet placed": "💰 Yes" if t["id"] in bet_tips else "",
        } for t in pending]), hide_index=True, width="stretch")
    else:
        st.info("Nothing waiting. Add today's picks in the **Analyse** tab.")

    st.markdown("#### 🏁 Recent results")
    settled = [t for t in db.list_tips(limit=60) if t["result"] != "PENDING"][:10]
    if settled:
        st.dataframe(pd.DataFrame([{
            "Date": fmt_date(t["match_date"]), "Source": source_label(t), "Match": t["match_name"],
            "Tip": f"{t['user_proposed_tip']} @ {fmt_odds(t['odds'])}",
            "Score": t["final_score"] or "", "Result": RESULT_BADGE[t["result"]],
            "AI said": ai_label(t),
        } for t in settled]), hide_index=True, width="stretch")
    else:
        st.caption("No settled picks yet.")

    st.markdown("#### 🧪 What's working? · last 30 days")
    st.caption("Each row bets 1 unit per pick. With fewer than ~200 settled picks, differences are mostly luck.")
    st.dataframe(pd.DataFrame([{
        "Approach": lab[k]["label"], "Settled": lab[k]["settled"], "Won": lab[k]["won"], "Lost": lab[k]["lost"],
        "Win rate": lab[k]["win_rate"] if lab[k]["settled"] else None,
        "Profit (units)": lab[k]["profit_units"] if lab[k]["settled"] else None, "ROI": lab[k]["roi"],
    } for k, _ in db.PERFORMANCE_CATEGORIES]), hide_index=True, width="stretch", column_config={
        "Win rate": st.column_config.NumberColumn(format="%.0f%%"),
        "Profit (units)": st.column_config.NumberColumn(format="%+.2f"),
        "ROI": st.column_config.NumberColumn(format="%+.1f%%")})
    st.markdown("#### 📈 Beating the closing price? · last 30 days")
    clv = db.get_clv_stats(user["id"])
    if not any(v[1] for v in clv.values()):
        st.caption("Enter **closing odds** (the price just before kickoff) in the Results tab. If the odds you take "
                   "are regularly above the closing odds, that's the earliest sign of a real edge - long before "
                   "profit can prove it.")
    else:
        names = {"tipster": "Listed odds", "mine": "My own picks", "my_bets": "Odds I actually got"}
        st.dataframe(pd.DataFrame([{"Odds": names[k], "Average vs closing": (f"{v[0]:+.1f}%" if v[1] else "—"),
                                    "Tips": v[1]} for k, v in clv.items()]), hide_index=True, width="stretch")
        st.caption("Positive = better than the market's final price (good). Consistently negative = no edge.")

    st.markdown("#### 🎯 Which forecaster is sharper?")
    acc = db.get_model_accuracy()
    if not acc["settled"]:
        st.caption("Shows once AI-analysed tips are settled. Lower score = better forecasts.")
    else:
        st.dataframe(pd.DataFrame([{"Forecaster": k, "Score (Brier, lower is better)": v[0], "Tips": v[1]}
                                   for k, v in acc.items() if k != "settled"]), hide_index=True, width="stretch")
        st.caption(f"Based on {acc['settled']} settled tips. If neither model beats 'Bookmaker odds' after "
                   "~100 tips, the AI isn't adding information. If one model is clearly better, keep only it "
                   "to halve the cost.")


# ---------------------------------------------------------------------------
# 2. Analyse (AI debate)
# ---------------------------------------------------------------------------
def start_todays_run(today):
    """Read today's tips and start their analysis now (same as the 13:00 scheduled run)."""
    try:
        rows, _ = ts.read_tips()
    except RuntimeError as exc:
        st.error(str(exc))
        return
    todays = [r for r in rows if r["date"] == today and not r["error"]][:4]
    if not todays:
        st.warning("Today's tips aren't posted yet - try again later.")
        return
    entries = [{"line": r["line"], "group": db.TIP_GROUPS[0], "slot_number": n} for n, r in enumerate(todays, 1)]
    db.update_setting("AUTO_RUN_DATE", today)  # the 13:00 automatic run then knows today is done
    ajobs.start(entries, "", today)
    st.rerun()


def analyse_tab():
    job = db.latest_job()
    if job and job["status"] == "running":
        job_progress(job["id"])
        return
    today = datetime.now(EAT).date().isoformat()
    job_tips = [t for t in (db.get_tip(i) for i in (job or {}).get("tip_ids", [])) if t]
    if any(str(t.get("match_date"))[:10] == today for t in job_tips):
        job_summary(job)
        return
    st.info("⏳ **Today's tips haven't been analysed yet.** They'll be ready after 13:00.")
    if st.button("▶️ Analyse today's tips now", width="stretch"):
        with st.spinner("Getting today's tips..."):
            start_todays_run(today)
    if job:
        st.markdown("#### Previous analysis")
        job_summary(job)


@st.fragment(run_every=3)
def job_progress(job_id):
    """Live progress of a background analysis. It keeps running on the server even if you lock your
    phone or close the page - coming back shows where it is."""
    job = db.get_job(job_id)
    if not job or job["status"] != "running":
        st.rerun()  # finished: redraw the whole tab with the results
    st.info("⏳ **Analysis running on the server.** You can lock your phone or leave this page - "
            "come back any time to see the results.")
    st.progress(job["done"] / max(job["total"], 1), text=f"{job['message'] or 'Starting...'} "
                                                       f"({job['done']}/{job['total']} tips)")
    for tid in job["tip_ids"]:
        t = db.get_tip(tid)
        if t:
            st.caption(f"✓ {t['match_name']} - {t['user_proposed_tip']} · {ai_label(t)}")


def top_pick(tips):
    """DeBet's pick of the day's tips: the one Grok and Claude together give the highest chance of winning,
    whatever its price. Tips where the models split are only used if nothing else was analysed."""
    rated = [t for t in tips if t.get("p_final") is not None]
    agreed = [t for t in rated if t.get("decision") != "SPLIT"] or rated
    return max(agreed, key=lambda t: (t["p_final"], -(t.get("model_gap") or 0)), default=None)


CLEAR_LEAD = 0.08  # the top tip must be at least 8 points above the next best to count as a clear pick


def pick_clarity(tips, pick):
    """'Clear' when DeBet's pick stands well above the next best tip, 'Close call' when they're similar."""
    rated = [t for t in tips if t.get("p_final") is not None and t.get("decision") != "SPLIT"
             and t["id"] != pick["id"]]
    if not rated:
        return ""
    runner = max(rated, key=lambda t: t["p_final"])
    lead = pick["p_final"] - runner["p_final"]
    who = f"{runner['match_name']} ({runner['p_final']:.0%})"
    if lead >= CLEAR_LEAD:
        return f"  \n🟢 **Clear pick** - {lead * 100:.0f} points ahead of the next best, {who}."
    return (f"  \n🟡 **Close call** - the next best, {who}, is only {lead * 100:.0f} point"
            f"{'' if round(lead * 100) == 1 else 's'} behind. DeBet can't clearly separate them: "
            "consider splitting your stake over the top tips as singles, or skipping today.")


def tip_line(t):
    return f"{t['match_name']} · **{t['user_proposed_tip']}** @ {fmt_odds(t['odds'])}"


def job_summary(job):
    """1) DeBet's pick of the day's tips (likeliest to win)  2) one alternative: the value pick, or a market to check."""
    message = job["message"] or ""
    tips = [t for t in (db.get_tip(i) for i in job["tip_ids"]) if t]
    # Judge the whole day together: tips for the same match day analysed in other runs (e.g. the automatic
    # 13:00 run of the tipster's 4, then your own 3 added later) join this run's tips.
    days = {str(t["match_date"])[:10] for t in tips if t.get("match_date")}
    ids = {t["id"] for t in tips}
    since = (datetime.now(timezone.utc) - timedelta(hours=36)).isoformat()
    tips += [t for t in db.list_tips(limit=60) if t["id"] not in ids and str(t.get("match_date"))[:10] in days
             and str(t["created_at"]) >= since and t.get("ai_verdict")]
    tips.sort(key=lambda t: t.get("slot_number") or 99)
    if job["status"] in ("failed", "interrupted"):
        st.error(message if job["status"] == "failed" else
                 "The last analysis was interrupted (the app restarted). Tips already finished are saved below; "
                 "run the rest again.")
    pick = top_pick(tips) if job["status"] == "done" else None
    best = max((t for t in tips if t.get("decision") == "BEST"), key=lambda t: t.get("value") or 0, default=None)
    if pick:
        p, odds = pick["p_final"], pick.get("odds")
        if odds:
            v = p * odds - 1
            price = (f"✅ Good price too: value **{v:+.0%}**." if v >= ve.VALUE_MIN else
                     f"⚠️ The price is short: value **{v:+.0%}** (fair odds {1 / p:.2f}).")
        else:
            price = f"❔ No odds entered - fair odds **{1 / p:.2f}**."
        stake = db.suggested_stake(st.session_state.get("user_id"))
        stake_txt = f"  \n💰 Suggested stake: **{fmt_money(stake)}**" if stake else ""
        st.success(f"🏆 **DeBet's pick of the {len(tips)} tips:** {tip_line(pick)}  \n"
                   f"Chance to win **{p:.0%}** (Grok {pick['p_grok']:.0%} · Claude {pick['p_claude']:.0%}) - "
                   f"the highest of the tips. {price}{pick_clarity(tips, pick)}{stake_txt}")
    # The one alternative
    if best and pick and best["id"] == pick["id"]:
        st.caption("⭐ It's also DeBet's value pick - it passed the challenge round.")
    elif best:
        st.info(f"💎 **Alternative - best value:** {tip_line(best)}  \nChance {best['p_final']:.0%} · "
                f"value **{best['value']:+.0%}** (passed the challenge round). Lower chance than the pick above, "
                "but a better price.")
    elif job["status"] == "done" and not best:
        alt = one_pick_to_check(tips)
        if alt:
            match, label, p, need = alt
            st.info(f"🔁 **Alternative market to check:** {match} · **{label}** ({p:.0%}) - worth it only at "
                    f"**{need:.2f} or higher**.")
    if message and job["status"] == "done" and not best:
        st.caption(f"Value check: {message}")
    started = datetime.fromisoformat(job["created_at"]).astimezone(EAT).strftime("%d %b %H:%M")
    st.markdown(f"#### Last run · {started} · AI cost ${job['cost_usd']:.3f}")
    for t in tips:
        tip_card(t)
    return {t["id"] for t in tips}


SHORT_MARKET = {"home": "1 Home", "draw": "X Draw", "away": "2 Away", "1x": "1X", "x2": "X2", "12": "12",
                "over15": "Over 1.5", "over25": "Over 2.5", "over35": "Over 3.5", "under15": "Under 1.5",
                "under25": "Under 2.5", "under35": "Under 3.5", "btts": "GG", "btts_no": "NG"}


def markets_table(probs):
    """All markets as a plain table that reads well on a phone. The tipped market is marked 👉 and bold."""
    pct = lambda v: f"{v:.0%}" if v is not None else "—"
    rows = ["| Market | Grok | Claude | DeBet | Fair |", "|:--|--:|--:|--:|--:|"]
    for k, p in probs["combined"].items():
        cells = [SHORT_MARKET.get(k, ve.MARKET_LABELS.get(k, k)), pct(probs["grok"].get(k)), pct(probs["claude"].get(k)),
                 pct(p), f"{1 / p:.2f}" if p else "—"]
        if k == probs.get("market_key"):
            cells = [f"**{c}**" for c in cells]
            cells[0] = "👉 " + cells[0]
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows)


def one_pick_to_check(tips):
    """On a no-bet day: the ONE market, across all the day's matches, to look up at the bookmaker.
    Candidates: every market both models agree on (within the usual gap) whose +5%-value price is at least the
    minimum odds - except tipped markets that already failed at their listed odds, and matches where the
    models split. The likeliest wins; ties go to the closer agreement. Returns (match, label, chance, odds) or None."""
    best = None
    for tip in tips:
        if not tip.get("probs_json") or tip.get("decision") == "SPLIT":
            continue
        probs = json.loads(tip["probs_json"])
        for k, p in probs["combined"].items():
            g, c = probs["grok"].get(k), probs["claude"].get(k)
            if not p or g is None or c is None or abs(g - c) > ve.MAX_MODEL_GAP:
                continue
            if k == probs.get("market_key") and tip.get("odds"):
                continue  # already checked at the tipster's price
            need = math.ceil((1 + ve.VALUE_MIN) / p * 100) / 100
            key = (p, -abs(g - c))
            if need >= ve.MIN_ODDS and (best is None or key > best[0]):
                best = (key, (tip["match_name"], ve.MARKET_LABELS.get(k, k), p, need))
    return best[1] if best else None


def tip_card(tip, result=None):
    """One tip: its price, what Grok and Claude think, the value and the decision. Details tucked away."""
    tid = tip["id"]
    with st.container(border=True):
        c = st.columns([2.2, 1.7, 2.0, 2.1])
        slot = f"#{tip['slot_number']} · " if tip.get("slot_number") else ""
        c[0].markdown(f"**{tip['match_name']}**  \n:gray[{source_label(tip)} · {slot}{fmt_date(tip['match_date'])}"
                      + (f" · {tip['competition']}" if tip.get("competition") else "") + "]")
        c[1].markdown(f"🎯 {tip['user_proposed_tip']}  \n**@ {fmt_odds(tip['odds'])}**")
        if tip.get("p_final") is not None:
            fair = 1 / tip["p_final"]
            c[2].markdown(f"Grok **{tip['p_grok']:.0%}** · Claude **{tip['p_claude']:.0%}**  \n"
                          f":gray[combined {tip['p_final']:.0%} → fair odds {fair:.2f}]")
        else:
            c[2].markdown(":gray[not analysed]")
        value = f"  \nvalue **{tip['value']:+.0%}**" if tip.get("value") is not None else ""
        c[3].markdown(DECISION_BADGE.get(tip.get("decision"), ai_label(tip)) + value)

        with st.expander("Why? (brief, each model's reasons, all markets)"):
            if not tip.get("ai_verdict"):
                st.error((tip.get("full_debate_log") or "Analysis failed.").removeprefix("Analysis failed: "))
            else:
                if (result or {}).get("date_warning"):
                    st.warning(result["date_warning"])
                if tip.get("decision") == "SPLIT":
                    st.warning(f"Grok and Claude are {tip['model_gap']:.0%} apart on this market - usually the brief "
                               "is missing something (e.g. an unconfirmed absence). Skipped.")
                if tip.get("challenge_note"):
                    st.markdown(f"**Challenge round:** {tip['challenge_note']}")
                if tip.get("probs_json"):
                    probs = json.loads(tip["probs_json"])
                    st.markdown("**All markets** - bet only if your bookmaker pays clearly more than the fair odds:")
                    st.markdown(markets_table(probs))
                    st.caption("GG = both teams score · NG = not both teams score · Fair = lowest odds worth taking")
                st.container(height=280).text(tip.get("full_debate_log") or "")
            if not tip.get("published_at") and st.button("🗑️ Delete this pick", key=f"del_{tid}"):
                db.delete_tip(tid)
                st.rerun()


# ---------------------------------------------------------------------------
# 3. My bets (real money)
# ---------------------------------------------------------------------------
def bankroll_box(user):
    start, pct = db.bankroll_settings()
    bank = db.current_bankroll(user["id"])
    stake = db.suggested_stake(user["id"])
    title = (f"💼 Bankroll {fmt_money(bank)} · suggested stake {fmt_money(stake)} ({pct:g}%)" if bank
             else "💼 Set your bankroll to get a suggested stake")
    with st.expander(title, expanded=not start):
        st.caption("Bet the same small share of your bankroll every time - never more after a loss. "
                   "Your bankroll updates automatically as your logged bets settle.")
        with st.form("bankroll"):
            c = st.columns(2)
            new_start = c[0].number_input("Starting bankroll", min_value=0.0, value=float(start), step=10000.0,
                                          format="%.0f")
            new_pct = c[1].number_input("Stake per bet (% of bankroll)", min_value=0.5, max_value=10.0,
                                        value=float(pct), step=0.5, format="%.1f",
                                        help="1-2% is the usual safe range.")
            if st.form_submit_button("Save"):
                try:
                    db.save_bankroll_settings(new_start, new_pct)
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    st.rerun()
    return stake


def bets_tab(user):
    suggested = bankroll_box(user)
    with st.container(border=True):
        st.markdown("#### 📝 Log a bet I placed")
        options = db.bettable_selections()
        if not options:
            st.caption("No upcoming picks with odds. Analyse picks first.")
        else:
            keys = {f"{o['tip']['id']}:{o['use']}": o for o in options}

            def label(k):
                o = keys[k]
                star = "⭐ " if o["tip"].get("decision") == "BEST" else ""
                return f"{star}{fmt_date(o['tip']['match_date'])} · {o['tip']['match_name']} — {o['market']} @ {o['odds']:.2f}"

            nonce = st.session_state.get("slip_nonce", 0)
            left, right = st.columns([3, 1])
            picked = left.multiselect("Selections (1 = single, up to 4 = accumulator)", list(keys),
                                      format_func=label, max_selections=4, key=f"slip_pick_{nonce}")
            stake = right.number_input("Stake", min_value=0.0, value=float(suggested or 0), step=500.0,
                                       format="%.0f", key=f"slip_stake_{nonce}",
                                       help="Pre-filled with your suggested stake." if suggested else None)
            taken = {}
            if picked:
                st.caption("Odds you actually got from your bookmaker (change them if different):")
                for k in picked:
                    o = keys[k]
                    taken[k] = st.number_input(f"{o['tip']['match_name']} — {o['market']}", min_value=1.01,
                                               max_value=1000.0, value=float(o["odds"]), step=0.01, format="%.2f",
                                               key=f"slip_odds_{nonce}_{k}")
            total = 1.0
            for k in picked:
                total *= taken.get(k) or keys[k]["odds"]
            if picked:
                st.caption(f"{db.BET_TYPES[len(picked)]} · total odds {total:.2f}"
                           + (f" · returns {fmt_money(stake * total)} if it wins" if stake > 0 else ""))
            if st.button("💾 Save bet", type="primary", disabled=not picked or stake <= 0, key=f"slip_save_{nonce}"):
                try:
                    bet = db.place_personal_bet(user["id"], [(*k.split(":"), taken.get(k)) for k in picked], stake)
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    st.session_state["slip_nonce"] = nonce + 1
                    st.toast(f"{bet['bet_type']} saved · {fmt_money(bet['stake_amount'])} @ {bet['total_odds']:.2f}")
                    st.rerun()

    labels = dict(db.BANKROLL_PERIODS)
    period = st.radio("Period", list(labels), index=1, format_func=labels.get, horizontal=True,
                      label_visibility="collapsed")
    s = db.get_user_bankroll_stats(user["id"], period)
    c = st.columns(4)
    c[0].metric("Profit / loss", fmt_signed(s["net_profit"]))
    c[1].metric("ROI", f"{s['roi']:+.1f}%" if s["roi"] is not None else "—")
    c[2].metric("Staked", fmt_money(s["total_staked"]))
    c[3].metric("Win rate", f"{s['win_rate']:.0f}%" if s["win_rate"] is not None else "—")
    st.caption(f"{s['total_slips']} bet(s) · {s['won']} won · {s['lost']} lost · {s['void']} void · "
               f"{s['pending']} waiting ({fmt_money(s['pending_staked'])} at stake)")

    bets = db.list_user_bets(user["id"])
    if not bets:
        st.info("No bets logged yet.")
        return
    st.dataframe(pd.DataFrame([{
        "Placed": datetime.fromisoformat(b["created_at"]).astimezone(EAT).strftime("%d %b %H:%M"), "Type": b["bet_type"], "Selections": b["selections_summary"],
        "Odds": b["total_odds"], "Stake": b["stake_amount"], "Status": RESULT_BADGE.get(b["status"], b["status"]),
        "Profit": b["net_profit"],
    } for b in bets]), hide_index=True, width="stretch", column_config={
        "Selections": st.column_config.TextColumn(width="large"),
        "Odds": st.column_config.NumberColumn(format="%.2f"),
        "Stake": st.column_config.NumberColumn(format="%.0f"),
        "Profit": st.column_config.NumberColumn(format="%+.0f")})
    pending = [b for b in bets if b["status"] == "PENDING"]
    if pending:
        with st.expander("Remove a waiting bet (typo)"):
            bid = st.selectbox("Bet", [b["id"] for b in pending], format_func=lambda i: next(
                f"{b['bet_type']} · {fmt_money(b['stake_amount'])} · {b['selections_summary']}" for b in pending if b["id"] == i))
            if st.button("🗑️ Remove"):
                db.delete_user_bet(user["id"], bid)
                st.rerun()


# ---------------------------------------------------------------------------
# 4. Results
# ---------------------------------------------------------------------------
def results_tab():
    last = rv.get_last_checked()
    c = st.columns([1, 2], vertical_alignment="center")
    if c[0].button("🔎 Check results now", type="primary", width="stretch"):
        with st.spinner("Looking up final scores..."):
            st.session_state["last_verification"] = rv.verify_pending_tips(force=True)
        st.rerun()
    c[1].caption(f"Last check: {last.strftime('%d %b %H:%M UTC') if last else 'never'}. "
                 "Never runs on its own. The button looks up scores with AI (about $0.005 per match) - "
                 "or just enter results yourself below for free.")
    summary = st.session_state.get("last_verification") or BOOT.get("startup_verification")
    if summary and not summary.get("skipped"):
        st.success(f"Checked {summary['checked']} · settled {len(summary['settled'])} · "
                   f"not finished {len(summary['not_played'])}" + (f" · {format_cost(summary['cost'])}" if summary.get("cost") else ""))
        for err in summary["errors"]:
            st.warning(f"{err['match']}: {err['error']}")

    st.markdown("#### ✏️ Results & closing odds")
    st.caption("**Closing odds** = the price for the same tip just before kickoff. Getting better odds than the "
               "closing price, again and again, is the earliest sign of a real edge.")
    flt = st.radio("Show", ["PENDING", "WON", "LOST", "VOID", "All"], horizontal=True, label_visibility="collapsed")
    tips = db.list_tips(result=None if flt == "All" else flt, limit=60)
    if not tips:
        st.info("Nothing here.")
    for t in tips:
        with st.form(f"ovr_{t['id']}"):
            c = st.columns([4, 2, 1.6, 1.6, 1], vertical_alignment="bottom")
            clv = (f" · vs close **{t['odds'] / t['closing_odds'] - 1:+.0%}**"
                   if t.get("closing_odds") and t.get("odds") else "")
            c[0].markdown(f"**{t['match_name']}** · {fmt_date(t['match_date'])}  \n"
                          f"🎯 {t['user_proposed_tip']} @ {fmt_odds(t['odds'])}{clv}")
            res = c[1].selectbox("Result", db.RESULTS, index=db.RESULTS.index(t["result"]), key=f"res_{t['id']}")
            score = c[2].text_input("Score", t["final_score"], placeholder="2-1", key=f"sc_{t['id']}")
            closing = c[3].number_input("Closing odds", min_value=1.01, max_value=1000.0, value=t.get("closing_odds"),
                                        step=0.01, format="%.2f", placeholder="at kickoff", key=f"co_{t['id']}")
            if c[4].form_submit_button("Save"):
                db.update_tip_result(t["id"], res, score.strip())
                if closing != t.get("closing_odds"):
                    db.set_closing_odds(t["id"], closing)
                st.toast("Saved")
                st.rerun()


# ---------------------------------------------------------------------------
# Client tiers: shared cards, your Clients tab, and the clients' own dashboard
# ---------------------------------------------------------------------------
TIERS = (("platinum", "💎 PLATINUM", "DeBet's best pick"), ("gold", "🥇 GOLD", "Second-best pick"),
         ("silver", "🥈 SILVER", "Alternative pick"))
OUTCOME_BADGE = {"WON": "✅ Won", "LOST": "❌ Lost", "VOID": "⚪ Void", None: "⏳ Pending"}


def selection_text(sel):
    price = (f"@ **{fmt_odds(sel['odds'])}**" if sel.get("odds")
             else f"· take only at **{sel['min_odds']:.2f}+**" if sel.get("min_odds") else "")
    chance = f" · chance {sel['chance']:.0%}" if sel.get("chance") is not None else ""
    return f"**{sel['match']}**  \n{sel['market']} {price}{chance}"


def tier_cards(day_data, show_results=False):
    for key, title, sub in TIERS:
        sel = day_data.get(key)
        if not sel:
            continue
        with st.container(border=True):
            result = f" · {OUTCOME_BADGE[ct.outcome(sel)]}" if show_results else ""
            st.markdown(f"{title} :gray[· {sub}]{result}  \n{selection_text(sel)}")
    acca = day_data.get("acca")
    if acca:
        with st.container(border=True):
            result = f" · {OUTCOME_BADGE[ct.acca_outcome(acca)]}" if show_results else ""
            legs = "  \n".join(f"• {l['match']} - **{l['market']}** ({l['chance']:.0%})" for l in acca["legs"])
            st.markdown(f"🎟️ **ACCUMULATOR** :gray[· {len(acca['legs'])} legs]{result}  \n{legs}  \n"
                        f"Chance all win: **{acca['chance']:.0%}** · worth it at combined odds "
                        f"**{acca['min_odds']:.2f}+**")


def clients_tab():
    """You: enter your own picks for clients, check the tiers, publish them, see who signed up."""
    st.caption("Type your picks and press Analyse - when the analysis finishes (1-2 minutes) the Platinum, Gold, "
               "Silver and accumulator go live to clients automatically. Only these picks are ever shown to "
               "clients. Each pick costs about $0.015 to analyse.")
    job = db.latest_job()
    if job and job["status"] == "running":
        job_progress(job["id"])
        return
    day = st.date_input("Match day", datetime.now(EAT).date(), format="YYYY-MM-DD", key="client_day").isoformat()
    with st.form("my_picks", clear_on_submit=True):
        text = st.text_area("My picks (one per line, with odds)", height=130,
                            placeholder="Arsenal vs Chelsea over 2.5 1.95\nRoma vs Lazio 1 2.10")
        go = st.form_submit_button("⚔️ Analyse my picks", type="primary")
    if go:
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        bad, parsed = [], []
        for line in lines:
            try:
                parsed.append((line, parse_tip(line)))
            except ValueError as exc:
                bad.append(f"`{line}`: {exc}")
        if bad:
            st.error("Fix these first:\n\n" + "\n\n".join(bad))
        elif parsed:
            dupes = db.find_duplicates([(p["match_name"], p["market"]) for _, p in parsed])
            fresh = [{"line": l, "group": db.TIP_GROUPS[1], "slot_number": 5 + i}
                     for i, (l, p) in enumerate(parsed) if (p["match_name"].lower(), p["market"].lower()) not in dupes]
            if dupes:
                st.warning(f"{len(parsed) - len(fresh)} pick(s) were already analysed recently and were skipped.")
            if fresh:
                ajobs.start(fresh, "", day)
                st.rerun()

    built = ct.build_day(day)
    saved = db.get_client_day(day)
    st.markdown(f"#### Tiers for {fmt_date(day)}")
    if not built:
        st.info("No analysed picks of yours for this day yet.")
    else:
        live = bool(saved and saved.get("published_at"))
        st.markdown(("🟢 **Live for clients** - published automatically when your picks finished analysing."
                     if live else "🙈 **Hidden from clients.**") + f" Based on {built['picks']} pick(s).")
        tier_cards(saved if live else built)
        if live and st.button("🙈 Hide from clients", width="stretch",
                              help="Emergency only. Analysing new picks for this day shows it again."):
            db.save_client_day(day, saved, publish=False)
            st.rerun()
        if not live and st.button("📣 Show to clients again", width="stretch"):
            db.save_client_day(day, built, publish=True)
            st.rerun()

    clients = db.list_users("subscriber")
    with st.expander(f"👥 Clients ({len(clients)})"):
        for u in clients:
            c = st.columns([3, 1], vertical_alignment="center")
            contact = u.get("email") or u["phone_number"]
            c[0].markdown(f"**{u['full_name']}** · {contact}  \n:gray[joined {fmt_date(u['created_at'])}]")
            if c[1].button("Remove", key=f"rm_{u['id']}"):
                db.delete_client(u["id"])
                st.rerun()
        if not clients:
            st.caption("Nobody has signed up yet.")


VIP_CSS = """<style>
:root {--vip-gold:#D4AF37; --vip-gold-hi:#F6E7B0; --vip-ink:#0E0E13; --vip-head:#17130A; --vip-line:#D4AF3733;
       --vip-muted:#B5AE98;}
.vip-hero {background:radial-gradient(120% 140% at 0% 0%, #2A2008 0%, #0E0E13 62%); border:1px solid var(--vip-line);
  border-radius:18px; padding:18px 18px 16px; margin:4px 0 6px; position:relative; overflow:hidden;}
.vip-hero:after {content:""; position:absolute; right:-40px; top:-40px; width:140px; height:140px; border-radius:50%;
  background:radial-gradient(circle, #D4AF3730, transparent 70%);}
.vip-kicker {font:700 .68rem Inter,sans-serif; letter-spacing:3px; color:var(--vip-gold); text-transform:uppercase;}
.vip-title {font-family:'Barlow Condensed',sans-serif; font-weight:800; font-size:2.1rem; line-height:1;
  color:var(--vip-gold-hi); margin-top:4px;}
.vip-date {color:var(--vip-muted); font-size:.85rem; margin-top:6px;}
.vip-sec {font-family:'Barlow Condensed',sans-serif; font-weight:700; font-size:1.1rem; letter-spacing:1.5px;
  text-transform:uppercase; color:var(--vip-gold); margin:22px 0 8px; display:flex; align-items:center; gap:8px;}
.vip-sec:after {content:""; flex:1; height:1px; background:linear-gradient(90deg, var(--vip-line), transparent);}
.vip-table {width:100%; border-collapse:separate; border-spacing:0; background:var(--vip-ink);
  border:1px solid var(--vip-line); border-radius:14px; overflow:hidden; font-size:.9rem;}
.vip-table th {background:var(--vip-head); color:var(--vip-gold); font:600 .66rem Inter,sans-serif;
  letter-spacing:1.5px; text-transform:uppercase; text-align:left; padding:10px; border-bottom:1px solid var(--vip-line);}
.vip-table td {padding:12px 10px; border-bottom:1px solid #FFFFFF0F; color:#EDEDED; vertical-align:middle;}
.vip-table tr:last-child td {border-bottom:none;}
.vip-table th, .vip-table td {border-left:none !important; border-right:none !important; border-top:none !important;}
.vip-table td.vip-sel {width:100%; word-break:break-word;}
.vip-day td {background:#15151C; color:var(--vip-gold); font:700 .7rem Inter,sans-serif; letter-spacing:1.5px;
  text-transform:uppercase; padding:7px 10px !important;}
[data-testid="stMarkdownContainer"]:has(.vip-table) {max-width:100%; overflow-x:auto;}
.vip-table .num {text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap;}
.vip-table th.num {text-align:right;}
.vip-sel b {display:block; color:#FFF; font-weight:600; line-height:1.25;}
.vip-sel span {color:var(--vip-muted); font-size:.8rem;}
.vip-odds {font-family:'Barlow Condensed',sans-serif; font-weight:700; font-size:1.2rem; color:var(--vip-gold-hi);}
.vip-min {color:var(--vip-muted); font-size:.75rem;}
.vip-badge {display:inline-block; padding:4px 8px; border-radius:999px; font:800 .62rem Inter,sans-serif;
  letter-spacing:1px; color:#111; white-space:nowrap;}
.b-platinum {background:linear-gradient(135deg,#FFFFFF,#C9D3E3 50%,#8E9BB3);}
.b-gold {background:linear-gradient(135deg,#FFF1B8,#D4AF37 55%,#A67C00);}
.b-silver {background:linear-gradient(135deg,#F2F2F2,#BFC3C7 55%,#8D9297);}
.b-acca {background:linear-gradient(135deg,#F3E8FF,#C4A7FF 55%,#8B5CF6);}
.vip-foot td {background:var(--vip-head); color:var(--vip-gold-hi); font-weight:600;}
.vip-chips {display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:8px; margin:2px 0 12px;}
.vip-chip {background:var(--vip-ink); border:1px solid var(--vip-line); border-radius:12px; padding:9px 10px;}
.vip-chip .t {font:700 .58rem Inter,sans-serif; letter-spacing:1.4px; color:var(--vip-muted); text-transform:uppercase;}
.vip-chip .v {font-family:'Barlow Condensed',sans-serif; font-weight:700; font-size:1.35rem; color:var(--vip-gold-hi);}
.vip-chip .s {color:var(--vip-muted); font-size:.72rem;}
.r-won {color:#4ADE80; font-weight:700;} .r-lost {color:#F87171; font-weight:700;}
.r-void, .r-pend {color:var(--vip-muted);}
.vip-empty {background:var(--vip-ink); border:1px dashed var(--vip-line); border-radius:14px; padding:22px;
  text-align:center; color:var(--vip-muted);}
.vip-note {color:#8F8A7A; font-size:.72rem; margin-top:18px; text-align:center;}
@media (max-width: 480px) {.vip-table {font-size:.84rem;} .vip-table td, .vip-table th {padding:9px 7px;}
  .vip-chips {grid-template-columns:repeat(2, minmax(0,1fr));}}
</style>"""
VIP_TIERS = (("platinum", "💎 PLATINUM"), ("gold", "🥇 GOLD"), ("silver", "🥈 SILVER"))
VIP_RESULT = {"WON": ('<span class="r-won">✓ Won</span>'), "LOST": '<span class="r-lost">✕ Lost</span>',
              "VOID": '<span class="r-void">Void</span>', None: '<span class="r-pend">Pending</span>'}


def _e(text):
    return html.escape(str(text or ""))


def _badge(key, label):
    return f'<span class="vip-badge b-{key}">{label}</span>'


def _sel_cell(sel):
    return f'<td class="vip-sel"><b>{_e(sel["match"])}</b><span>{_e(sel["market"])}</span></td>'


def _odds_cell(sel):
    if sel.get("odds"):
        return f'<td class="num"><span class="vip-odds">{sel["odds"]:.2f}</span></td>'
    return f'<td class="num"><span class="vip-odds">{sel["min_odds"]:.2f}+</span><br><span class="vip-min">min</span></td>'


def vip_today_table(day):
    rows = "".join(f"<tr><td>{_badge(k, label)}</td>{_sel_cell(day[k])}{_odds_cell(day[k])}</tr>"
                   for k, label in VIP_TIERS if day.get(k))
    return ('<table class="vip-table"><tr><th>Tier</th><th>Selection</th><th class="num">Odds</th></tr>'
            f'{rows}</table>')


def vip_acca_table(acca):
    rows = "".join(f'<tr><td class="num">{i}</td>{_sel_cell(l)}</tr>' for i, l in enumerate(acca["legs"], 1))
    return ('<table class="vip-table"><tr><th class="num">Leg</th><th>Selection</th></tr>'
            f'{rows}<tr class="vip-foot"><td></td><td>Take combined odds <b>{acca["min_odds"]:.2f}+</b></td></tr>'
            '</table>')


def vip_results_table(past):
    rows = []
    for d in past:
        entries = [(k, label, d.get(k), ct.outcome(d.get(k))) for k, label in VIP_TIERS if d.get(k)]
        if d.get("acca"):
            acca = d["acca"]
            entries.append(("acca", "🎟️ ACCA", {"match": f"{len(acca['legs'])}-leg accumulator",
                                               "market": " + ".join(l["market"] for l in acca["legs"])},
                            ct.acca_outcome(acca)))
        if entries:
            rows.append(f'<tr class="vip-day"><td colspan="3">{_e(fmt_date(d["day"]))}</td></tr>')
        for k, label, sel, res in entries:
            rows.append(f'<tr><td>{_badge(k, label.split()[-1])}</td>{_sel_cell(sel)}'
                        f'<td class="num">{VIP_RESULT[res]}</td></tr>')
    return ('<table class="vip-table"><tr><th>Tier</th><th>Selection</th><th class="num">Result</th></tr>'
            + "".join(rows) + "</table>")


def vip_record(past):
    chips = []
    for k, label in VIP_TIERS + (("acca", "🎟️ ACCA"),):
        res = [ct.acca_outcome(d.get(k)) if k == "acca" else ct.outcome(d.get(k)) for d in past if d.get(k)]
        won, lost = res.count("WON"), res.count("LOST")
        rate = f"{won / (won + lost):.0%}" if won + lost else "—"
        chips.append(f'<div class="vip-chip"><div class="t">{label.split()[-1]}</div><div class="v">{rate}</div>'
                     f'<div class="s">{won} won · {lost} lost</div></div>')
    return f'<div class="vip-chips">{"".join(chips)}</div>'


def _money_cell(v):
    cls = "r-won" if v > 0 else "r-lost" if v < 0 else "r-void"
    return f'<span class="{cls}">{fmt_signed(v)}</span>'


def client_bets_tab(user):
    """A client's own betting record on DeBet's picks: log bets, see if they're profitable."""
    show = lambda h: st.markdown(h, unsafe_allow_html=True)
    today = datetime.now(EAT).date().isoformat()
    # what can still be bet on: published days from today on, picks whose matches haven't been played
    options = {}
    for d in db.list_published_days(10):
        if d["day"] < today:
            continue
        for k, label in VIP_TIERS + (("acca", "🎟️ ACCUMULATOR"),):
            sel = d.get(k)
            if not sel:
                continue
            legs = sel["legs"] if k == "acca" else [sel]
            if any((db.get_tip(l["tip_id"]) or {}).get("final_score") for l in legs):
                continue
            what = (f"{len(legs)} legs" if k == "acca" else f"{sel['match']} - {sel['market']}")
            price = sel.get("odds") or sel.get("min_odds")
            options[(d["day"], k)] = (f"{label} · {what}" + ("" if d["day"] == today else f" ({fmt_date(d['day'])})"),
                                      price)

    show('<div class="vip-sec">Log a bet</div>')
    if not options:
        show('<div class="vip-empty">When today\'s VIP tips are posted, you can log your bets on them here.</div>')
    else:
        choice = st.selectbox("Which tip did you bet on?", list(options), format_func=lambda o: options[o][0],
                              key="cb_choice")
        with st.form("client_bet", clear_on_submit=True):
            c = st.columns(2)
            stake = c[0].number_input("Stake", min_value=0.0, step=500.0, format="%.0f")
            odds = c[1].number_input("Odds you got", min_value=1.01, max_value=10000.0, step=0.01, format="%.2f",
                                     value=float(options[choice][1] or 1.5), key=f"cb_odds_{choice[0]}_{choice[1]}")
            if st.form_submit_button("➕ Add bet", type="primary", width="stretch"):
                try:
                    db.place_client_bet(user["id"], choice[0], choice[1], stake, odds)
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    st.toast("Bet added")
                    st.rerun()

    period = st.segmented_control("Period", ["7 days", "30 days", "All time"], default="30 days",
                                  key="cb_period", label_visibility="collapsed") or "30 days"
    days = {"7 days": 7, "30 days": 30, "All time": 36500}[period]
    s = db.get_user_bankroll_stats(user["id"], days)
    net = s["net_profit"]
    if s["won"] + s["lost"]:
        verdict = (f'<span class="r-won">In profit ✓</span>' if net > 0 else
                   f'<span class="r-lost">Losing ✕</span>' if net < 0 else '<span class="r-void">Break-even</span>')
    else:
        verdict = '<span class="r-void">No settled bets yet</span>'
    roi = f"{s['roi']:+.0f}%" if s["roi"] is not None else "—"
    win_rate = f"{s['win_rate']:.0f}%" if s["win_rate"] is not None else "—"
    chip = lambda title, value, sub: (f'<div class="vip-chip"><div class="t">{title}</div><div class="v">{value}</div>'
                                      f'<div class="s">{sub}</div></div>')
    show(f'<div class="vip-sec">My performance · {period.lower()}</div><div class="vip-chips">'
         + chip("Profit", _money_cell(net), verdict)
         + chip("Return", roi, f"on {fmt_money(s['settled_staked'])} staked")
         + chip("Win rate", win_rate, f"{s['won']} won · {s['lost']} lost")
         + chip("Open bets", s["pending"], f"{fmt_money(s['pending_staked'])} at stake")
         + "</div>")

    bets = db.list_user_bets(user["id"], days=None if days > 3650 else days)
    settled = sorted((b for b in bets if b["status"] != "PENDING"), key=lambda b: b["created_at"])
    if len(settled) >= 2:
        running, points = 0.0, []
        for b in settled:
            running += b["net_profit"] or 0
            points.append({"Bet": len(points) + 1, "Profit": round(running, 2)})
        show('<div class="vip-sec">Profit over time</div>')
        st.line_chart(pd.DataFrame(points).set_index("Bet"), height=180, color="#D4AF37")

    if bets:
        tiers = {}
        for b in bets:
            tier = (json.loads(b["legs"] or "[]") or [{}])[0].get("tier")
            if tier and b["status"] in ("WON", "LOST", "VOID"):
                t = tiers.setdefault(tier, [0, 0, 0.0])
                t[0] += b["status"] == "WON"
                t[1] += b["status"] == "LOST"
                t[2] += b["net_profit"] or 0
        if tiers:
            rows = "".join(f'<tr><td>{_badge(k, db.CLIENT_TIER_NAMES[k] if k != "acca" else "ACCA")}</td>'
                           f'<td class="num">{w}–{l}</td><td class="num">{_money_cell(p)}</td></tr>'
                           for k in ("platinum", "gold", "silver", "acca") if k in tiers
                           for w, l, p in [tiers[k]])
            show('<div class="vip-sec">By tier</div><table class="vip-table"><tr><th>Tier</th>'
                 f'<th class="num">Won–lost</th><th class="num">Profit</th></tr>{rows}</table>')

        rows = []
        for b in bets:
            legs = json.loads(b["legs"] or "[]")
            tier = (legs or [{}])[0].get("tier") or "single"
            if tier == "acca":
                title, market = f"Accumulator · {len(legs)} legs", ""
            elif legs:
                title, market = legs[0]["match"], f" · {legs[0]['market']}"
            else:
                title, market = b["selections_summary"], ""
            when = datetime.fromisoformat(b["created_at"]).astimezone(EAT).strftime("%d %b")
            result = (_money_cell(b["net_profit"]) if b["status"] in ("WON", "LOST")
                      else VIP_RESULT["VOID" if b["status"] == "VOID" else None])
            badge = _badge(tier, {"acca": "ACCA"}.get(tier, db.CLIENT_TIER_NAMES.get(tier, "BET")))
            rows.append(f'<tr><td class="vip-sel"><b>{_e(title)}</b><span>{when}{_e(market)} · '
                        f'{fmt_money(b["stake_amount"])} @ {b["total_odds"]:.2f}</span></td>'
                        f'<td>{badge}</td><td class="num">{result}</td></tr>')
        show('<div class="vip-sec">My bets</div><table class="vip-table"><tr><th>Bet</th><th>Tier</th>'
             f'<th class="num">Result</th></tr>{"".join(rows)}</table>')

        pending = [b for b in bets if b["status"] == "PENDING"]
        if pending:
            with st.expander("Remove an open bet (typo)"):
                bid = st.selectbox("Bet", [b["id"] for b in pending], key="cb_rm",
                                   format_func=lambda i: next(b["selections_summary"] for b in pending if b["id"] == i))
                if st.button("🗑️ Remove", key="cb_rm_btn"):
                    db.delete_user_bet(user["id"], bid)
                    st.rerun()
    else:
        show('<div class="vip-empty" style="margin-top:12px">Your bets and profit will show here.</div>')


def client_main(user):
    """What a client sees, VIP style: today's tips (record, results) and their own bets."""
    st.markdown(VIP_CSS, unsafe_allow_html=True)
    head, menu = st.container(key="topbar").columns([5, 1], vertical_alignment="center")
    head.markdown('<div class="db-brand">DE<span>BET</span></div>', unsafe_allow_html=True)
    with menu.popover("⚙️", width="stretch"):
        st.caption(f"Logged in as {user['full_name']}")
        if st.button("Log out", width="stretch"):
            logout()
    t_tips, t_bets = st.tabs(["⭐ VIP Tips", "💰 My Bets"])
    with t_bets:
        client_bets_tab(user)
    with t_tips:
        client_tips(user)


def client_tips(user):
    now = datetime.now(EAT)
    today = now.date().isoformat()
    days = db.list_published_days(30)
    current = min((d for d in days if d["day"] >= today), key=lambda d: d["day"], default=None)
    past = [d for d in days if d["day"] < today]
    for_day = "" if not current or current["day"] == today else f" · tips for {fmt_date(current['day'])}"
    show = st.markdown
    show(f'<div class="vip-hero"><div class="vip-kicker">★ VIP members</div><div class="vip-title">Today\'s selections'
         f'</div><div class="vip-date">{now:%A %d %B %Y}{_e(for_day)}</div></div>', unsafe_allow_html=True)

    if current:
        show('<div class="vip-sec">Daily picks</div>' + vip_today_table(current), unsafe_allow_html=True)
        if current.get("acca"):
            show('<div class="vip-sec">🎟️ VIP accumulator</div>' + vip_acca_table(current["acca"]),
                 unsafe_allow_html=True)
    else:
        show('<div class="vip-empty">Today\'s VIP selections will appear here once they\'re ready.</div>',
             unsafe_allow_html=True)

    show('<div class="vip-sec">VIP record · last 30 days</div>' + vip_record(past), unsafe_allow_html=True)
    if past:
        show('<div class="vip-sec">Recent results</div>' + vip_results_table(past[:14]), unsafe_allow_html=True)
    show('<div class="vip-note">18+ only · Tips are opinions, not guarantees · Never bet more than you can afford '
         'to lose · <a href="?page=terms">Terms</a> · <a href="?page=privacy">Privacy</a></div>',
         unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
LEGAL_PAGES = {
    "privacy": ("Privacy policy", """
**What we collect.** Your phone number and a password (stored only as a secure scrambled code).

**Why.** Only to create your account and let you sign in to see the daily tips and results.

**Sharing.** We never sell or share your information with anyone.

**Storage.** Your details are stored in our database (Supabase) until you ask us to delete your account.

**Deleting your account.** Ask the DeBet admin and your account and data will be removed.

**Cookies.** One cookie keeps you signed in on your device.
"""),
    "terms": ("Terms of service", """
**18+ only.** You must be 18 or older to use DeBet.

**Tips are opinions.** Tips, chances and accumulators are opinions based on analysis, never guarantees.
Football is unpredictable and you can lose money.

**Your responsibility.** You decide whether to bet and how much. Never bet more than you can afford to lose.
DeBet is not responsible for any losses.

**Accounts.** We may remove any account at any time.

**Help.** If betting stops being fun, take a break and talk to someone you trust.
"""),
}


def legal_page(page):
    title, body = LEGAL_PAGES[page]
    st.markdown('<div class="db-brand">DE<span>BET</span></div>', unsafe_allow_html=True)
    st.markdown(f"## {title}")
    st.markdown(body)
    st.caption("[Back](/)")


def main():
    st.markdown(THEME_CSS, unsafe_allow_html=True)
    page = st.query_params.get("page")
    if page in LEGAL_PAGES:  # public pages - no login needed
        legal_page(page)
        return
    user = current_user()
    inject_page_scripts()  # after current_user(), so the login cookie matches this run
    if not user:
        login_page()
        return
    if user["role"] != "admin":  # clients only ever get the client dashboard
        client_main(user)
        return

    head, menu = st.container(key="topbar").columns([5, 1], vertical_alignment="center")
    head.markdown('<div class="db-brand">DE<span>BET</span></div>', unsafe_allow_html=True)
    with menu.popover("⚙️", width="stretch"):
        with st.form("pw", clear_on_submit=True):
            p1 = st.text_input("New password", type="password")
            p2 = st.text_input("Repeat", type="password")
            if st.form_submit_button("Change password"):
                if len(p1) < 8:
                    st.error("At least 8 characters.")
                elif p1 != p2:
                    st.error("They don't match.")
                else:
                    db.change_password(user["id"], p1)
                    start_session(user["id"])
                    st.success("Changed. Other devices are logged out.")
        st.markdown("**Backup**")
        if st.button("📦 Prepare backup", width="stretch",
                     help="All tips, bets, settings and AI costs as spreadsheet (CSV) files in one .zip."):
            st.session_state["backup_zip"] = db.export_backup_zip()
        if st.session_state.get("backup_zip"):
            st.download_button("⬇️ Download backup", st.session_state["backup_zip"],
                               file_name=f"debet-backup-{datetime.now(EAT):%Y-%m-%d}.zip", mime="application/zip",
                               width="stretch", on_click="ignore")
        st.divider()
        if st.button("Log out", width="stretch"):
            logout()

    t1, t2, t5, t3, t4 = st.tabs(["📊 Dashboard", "⚔️ Analyse", "👥 Clients", "💰 My Bets", "✅ Results"])
    with t1:
        dashboard(user)
    with t2:
        analyse_tab()
    with t5:
        clients_tab()
    with t3:
        bets_tab(user)
    with t4:
        results_tab()


main()

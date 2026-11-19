"""DeBet - personal AI-checked football betting desk (single user). Run with:  streamlit run app.py

The old multi-user/subscriber version is kept in archive/app_multiuser.py.
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
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
import result_verifier as rv
import screenshot_reader as sr
import value_engine as ve
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
GROUP_ROWS = ((db.TIP_GROUPS[0], "Tipster's 4 tips", range(1, 5)),
              (db.TIP_GROUPS[1], "My own picks (optional)", range(5, 8)))
SHOT_TARGETS = {"Tipster's slots (#1–#4)": range(1, 5), "My slots (#5–#7)": range(5, 8)}
DEBATE_WORKERS = 2  # 3 parallel debates triggered OpenRouter "402 ... in-flight requests" errors
EAT = timezone(timedelta(hours=3))


# ---------------------------------------------------------------------------
# Startup (once per server process)
# ---------------------------------------------------------------------------
@st.cache_resource
def bootstrap():
    db.init_db()  # first start only: prints a generated admin password to the server console
    state = {"startup_verification": None}

    def _verify():  # background, throttled to once every 3 hours
        state["startup_verification"] = rv.auto_verify_on_startup()

    threading.Thread(target=_verify, daemon=True).start()
    return state


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
    """The logged-in admin, or None. Non-admin accounts (old subscribers) can't get in."""
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
    if not user or user["role"] != "admin" or (token and not db.get_session_user(token)):
        logout(rerun=False)
        return None
    return user


def login_page():
    st.markdown('<div class="db-brand">DE<span>BET</span></div>'
                '<div class="db-kicker">Personal betting desk</div>', unsafe_allow_html=True)
    _, mid, _ = st.columns([1, 2, 1])
    with mid, st.form("login"):
        ident = st.text_input("Login", placeholder="admin@debet.local")
        pw = st.text_input("Password", type="password")
        if st.form_submit_button("Log in", type="primary", width="stretch"):
            locked = db.login_lock_minutes_left(ident)
            if locked:
                st.error(f"Too many wrong passwords. Try again in {locked} minute(s).")
                st.stop()
            user = db.login_user(ident, pw)
            if user and user["role"] == "admin":
                db.clear_login_failures(ident)
                start_session(user["id"])
                st.rerun()
            db.record_login_failure(ident)
            st.error("Wrong login or password.")
        st.caption("Forgot it? On the computer running DeBet: `python database.py --reset-admin-password`")


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
    c[0].metric("Real money · 30 days", fmt_signed(money["net_profit"]),
                f"ROI {money['roi']:+.1f}%" if money["roi"] is not None else None,
                help="From the bets you logged in My Bets (settled slips only).")
    c[1].metric("Tipster's 4 · win rate", f"{tipster['win_rate']:.0f}%" if tipster["settled"] else "—",
                units(tipster), help="Every tipster tip from the last 30 days, 1 unit each at their odds.")
    c[2].metric("AI best pick · win rate", f"{best['win_rate']:.0f}%" if best["settled"] else "—", units(best),
                help="Only the one tip a day the AI picked as best value (or none).")
    c[3].metric("Waiting for result", lab["raw_all"]["pending"],
                help="Tips from the last 30 days that haven't been settled yet.")

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
def analyse_tab():
    st.caption("Type one tip per line with its odds, e.g. `Arsenal vs Chelsea over 2.5 1.95`. Grok and Claude each "
               f"estimate the chances without seeing the odds; DeBet then picks the best value tip (at least "
               f"+{ve.VALUE_MIN:.0%}) or says **No bet today**. Tips under {ve.MIN_ODDS:.2f} are recorded but not analysed.")
    if "studio_date" not in st.session_state:
        st.session_state["studio_date"] = datetime.now(EAT).date()
    screenshot_box()
    entries = []
    with st.form("studio"):
        match_date = st.date_input("Match date (used if the AI can't confirm it)", key="studio_date",
                                   format="YYYY-MM-DD")
        cols = st.columns(2, gap="large")
        for col, (group, title, slots) in zip(cols, GROUP_ROWS):
            with col:
                st.markdown(f"**{title}**")
                for n in slots:
                    line = st.text_input(f"#{n}", key=f"tip_row_{n}", label_visibility="collapsed",
                                         placeholder=f"#{n}  e.g. Arsenal vs Chelsea over 2.5 1.95" if n in (1, 5) else f"#{n}")
                    entries.append({"slot_number": n, "group": group, "line": line.strip()})
        notes = st.text_input("Notes for the AI (optional)")
        run = st.form_submit_button("⚔️ Analyse picks", type="primary")

    if run:
        filled = [e for e in entries if e["line"]]
        bad = []
        for e in filled:
            try:
                parse_tip(e["line"])
            except ValueError as exc:
                bad.append(f"#{e['slot_number']} `{e['line']}`: {exc}")
        if not filled:
            st.error("Type at least one pick.")
        elif bad:
            st.error("Fix these first:\n\n" + "\n\n".join(bad))
        else:
            run_debates(filled, notes, match_date)

    results = st.session_state.get("studio_results") or []
    if results:
        spent = sum((r.get("cost") or {}).get("total_usd") or 0 for r in results) \
            + ((st.session_state.get("day_cost") or {}).get("total_usd") or 0)
        message = st.session_state.get("day_decision") or ""
        (st.success if message.startswith("Best pick") else st.info)(f"**{message}**")
        st.markdown(f"#### Results of this run · AI cost ${spent:.3f}")
        for r in sorted(results, key=lambda r: r["slot_number"]):
            tip = db.get_tip(r["tip_id"]) if r.get("tip_id") else None
            if tip:
                tip_card(tip, r.get("result"))
            else:
                st.error(f"#{r['slot_number']} {r['line']} - failed and not saved: {r.get('error')}")

    shown = {r.get("tip_id") for r in results}
    recent = [t for t in db.list_tips(result="PENDING", limit=40) if t["id"] not in shown]
    if recent:
        st.markdown("#### Earlier picks still waiting for a result")
        for t in recent:
            tip_card(t)


def screenshot_box():
    """Upload the tipster's screenshot(s); Claude reads the table and fills the slots for you to check."""
    with st.expander("📸 Fill the slots from a screenshot", expanded=not st.session_state.get("shot_rows")):
        nonce = st.session_state.get("shot_nonce", 0)
        files = st.file_uploader("Screenshot(s) of the tips table", type=["png", "jpg", "jpeg", "webp"],
                                 accept_multiple_files=True, key=f"shot_files_{nonce}",
                                 help=f"Up to {sr.MAX_IMAGES} images. On a phone, pick the screenshot from your gallery.")
        target = st.radio("Put the tips in", list(SHOT_TARGETS), horizontal=True, key="shot_target")
        if st.button("🔎 Read tips", disabled=not files, key="shot_read"):
            with st.spinner("Reading the screenshot..."):
                try:
                    rows, unreadable, cost = sr.read_tips([(f.getvalue(), f.type) for f in files],
                                                          datetime.now(EAT).date())
                except Exception as exc:
                    st.error(str(exc))
                    rows = None
            if rows is not None:
                st.session_state["shot_rows"] = {"rows": rows, "unreadable": unreadable,
                                                 "cost": (cost or {}).get("total_usd")}

        found = st.session_state.get("shot_rows")
        if not found:
            return
        rows = found["rows"]
        if not rows:
            st.warning("No tips found in that screenshot.")
            return
        dates = sorted({r["date"] for r in rows if r["date"]}, reverse=True)
        day = st.radio("Which day?", dates, horizontal=True, format_func=fmt_date, key="shot_day") if len(dates) > 1 \
            else (dates[0] if dates else None)
        picked = [r for r in rows if r["date"] == day or not dates]
        slots = list(SHOT_TARGETS[target])
        st.caption(f"Found {len(rows)} tip(s)" + (f" · cost ${found['cost']:.3f}" if found.get("cost") else "")
                   + f". These go into slots #{slots[0]}–#{slots[-1]} - check them before analysing.")
        for r in picked:
            st.markdown(("⚠️ " if r["error"] else "✅ ") + f"`{r['line']}`" + (f" - {r['error']}" if r["error"] else ""))
        for u in found["unreadable"]:
            st.caption(f"Couldn't read clearly: {u}")
        good = [r for r in picked if not r["error"]]
        if len(good) > len(slots):
            st.warning(f"Only the first {len(slots)} fit in these slots.")
        if st.button(f"⬇️ Put {min(len(good), len(slots))} tip(s) in the slots", type="primary",
                     disabled=not good, key="shot_fill"):
            for n in slots:
                st.session_state[f"tip_row_{n}"] = ""
            for n, r in zip(slots, good):
                st.session_state[f"tip_row_{n}"] = r["line"]
            if day:
                st.session_state["studio_date"] = date.fromisoformat(day)
            st.session_state.pop("shot_rows", None)
            st.session_state["shot_nonce"] = nonce + 1  # clears the uploader
            st.rerun()


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
                    pct = lambda v: f"{v:.0%}" if v is not None else "—"
                    st.dataframe(pd.DataFrame([{
                        "Market": ("👉 " if k == probs.get("market_key") else "") + ve.MARKET_LABELS.get(k, k),
                        "Grok": pct(probs["grok"].get(k)), "Claude": pct(probs["claude"].get(k)),
                        "Combined": pct(p), "Fair odds": f"{1 / p:.2f}" if p else "—"}
                        for k, p in probs["combined"].items()]), hide_index=True, width="stretch")
                st.container(height=280).text(tip.get("full_debate_log") or "")
            if not tip.get("published_at") and st.button("🗑️ Delete this pick", key=f"del_{tid}"):
                db.delete_tip(tid)
                st.session_state["studio_results"] = [r for r in st.session_state.get("studio_results") or []
                                                      if r.get("tip_id") != tid]
                st.rerun()


def run_debates(entries, notes, match_date):
    """Brief + Grok + Claude for each tip (2 tips at a time), then one challenge round on the day's best
    candidate. Every tip is saved - skipped, failed or not - so the tipster's record stays complete."""
    default_date = match_date.isoformat() if match_date else None
    progress = st.progress(0.0, text=f"Analysing {len(entries)} tip(s) - this takes a few minutes...")
    done_entries = []
    with ThreadPoolExecutor(max_workers=DEBATE_WORKERS) as pool:
        futures = {pool.submit(ve.analyze_tip, e["line"], notes, default_date): e for e in entries}
        for done, fut in enumerate(as_completed(futures), 1):
            e = {**futures[fut]}
            try:
                e["result"] = fut.result()
                e["cost"] = e["result"].get("cost")
                print(f"[DeBet cost] {e['line']}: {format_cost(e['cost'])}", flush=True)
            except Exception as exc:
                e["error"], e["cost"] = str(exc), getattr(exc, "debate_cost", None)
                print(f"[DeBet] #{e['slot_number']} {e['line']} FAILED: {exc}", flush=True)
            done_entries.append(e)
            progress.progress(done / len(entries), text=f"Done {done}/{len(entries)}: {e['line']}")

    progress.progress(1.0, text="Checking the best candidate...")
    results = [e.get("result") for e in done_entries]
    try:
        _, message, day_cost = ve.choose_best(results)
    except Exception as exc:  # never lose the analyses because the last step failed
        message, day_cost = f"No bet today - the challenge round failed ({exc}).", None
    progress.empty()

    for e in done_entries:
        tip = parse_tip(e["line"])
        db.delete_failed_duplicates(tip["match_name"], tip["market"])
        try:
            if e.get("result"):
                fields = ve.tip_fields(e["result"], e["group"], e["slot_number"], default_date)
            else:
                fields = {"match_name": tip["match_name"], "user_proposed_tip": tip["market"], "odds": tip["odds"],
                          "tip_group": e["group"], "slot_number": e["slot_number"], "match_date": default_date,
                          "full_debate_log": f"Analysis failed: {e['error']}"}
            e["tip_id"] = db.save_tip(**fields)["id"]
        except Exception as exc:
            e.update(tip_id=None, error=f"Saving failed: {exc}")
    st.session_state["studio_results"] = done_entries
    st.session_state["day_decision"] = message
    st.session_state["day_cost"] = day_cost


# ---------------------------------------------------------------------------
# 3. My bets (real money)
# ---------------------------------------------------------------------------
def bets_tab(user):
    with st.container(border=True):
        st.markdown("#### 📝 Log a bet I placed")
        options = db.bettable_selections()
        if not options:
            st.caption("No upcoming picks with odds. Analyse picks first (AI alternatives need their real odds).")
        else:
            keys = {f"{o['tip']['id']}:{o['use']}": o for o in options}

            def label(k):
                o = keys[k]
                tag = "🤖 " if o["use"] == "alternative" else ""
                return f"{fmt_date(o['tip']['match_date'])} · {o['tip']['match_name']} — {tag}{o['market']} @ {o['odds']:.2f}"

            nonce = st.session_state.get("slip_nonce", 0)
            left, right = st.columns([3, 1])
            picked = left.multiselect("Selections (1 = single, up to 4 = accumulator)", list(keys),
                                      format_func=label, max_selections=4, key=f"slip_pick_{nonce}")
            stake = right.number_input("Stake", min_value=0.0, value=0.0, step=1000.0, format="%.0f",
                                       key=f"slip_stake_{nonce}")
            total = 1.0
            for k in picked:
                total *= keys[k]["odds"]
            if picked:
                st.caption(f"{db.BET_TYPES[len(picked)]} · total odds {total:.2f}"
                           + (f" · returns {fmt_money(stake * total)} if it wins" if stake > 0 else ""))
            if st.button("💾 Save bet", type="primary", disabled=not picked or stake <= 0, key=f"slip_save_{nonce}"):
                try:
                    bet = db.place_personal_bet(user["id"], [k.split(":") for k in picked], stake)
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
                 "Also runs automatically (at most every 3 hours) when the app starts.")
    summary = st.session_state.get("last_verification") or BOOT.get("startup_verification")
    if summary and not summary.get("skipped"):
        st.success(f"Checked {summary['checked']} · settled {len(summary['settled'])} · "
                   f"not finished {len(summary['not_played'])}" + (f" · {format_cost(summary['cost'])}" if summary.get("cost") else ""))
        for err in summary["errors"]:
            st.warning(f"{err['match']}: {err['error']}")

    st.markdown("#### ✏️ Correct a result")
    flt = st.radio("Show", ["PENDING", "WON", "LOST", "VOID", "All"], horizontal=True, label_visibility="collapsed")
    tips = db.list_tips(result=None if flt == "All" else flt, limit=60)
    if not tips:
        st.info("Nothing here.")
    for t in tips:
        with st.form(f"ovr_{t['id']}"):
            c = st.columns([4, 2, 2, 1.5, 1], vertical_alignment="bottom")
            c[0].markdown(f"**{t['match_name']}** · {fmt_date(t['match_date'])}  \n"
                          f"🎯 {t['user_proposed_tip']} @ {fmt_odds(t['odds'])}"
                          + (f"  \n🤖 {t['alt_market']}" if t.get("alt_market") else ""))
            res = c[1].selectbox("My pick", db.RESULTS, index=db.RESULTS.index(t["result"]), key=f"res_{t['id']}")
            alt_res = c[2].selectbox("AI alternative", db.RESULTS, index=db.RESULTS.index(t["alt_result"]),
                                     key=f"alt_{t['id']}", disabled=not t.get("alt_market"))
            score = c[3].text_input("Score", t["final_score"], placeholder="2-1", key=f"sc_{t['id']}")
            if c[4].form_submit_button("Save"):
                db.update_tip_result(t["id"], res, score.strip(), alt_res if t.get("alt_market") else None)
                st.toast("Saved")
                st.rerun()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    st.markdown(THEME_CSS, unsafe_allow_html=True)
    user = current_user()
    inject_page_scripts()  # after current_user(), so the login cookie matches this run
    if not user:
        login_page()
        return

    head, menu = st.columns([5, 1], vertical_alignment="center")
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
        if st.button("Log out", width="stretch"):
            logout()

    t1, t2, t3, t4 = st.tabs(["📊 Dashboard", "⚔️ Analyse", "💰 My Bets", "✅ Results"])
    with t1:
        dashboard(user)
    with t2:
        analyse_tab()
    with t3:
        bets_tab(user)
    with t4:
        results_tab()


main()

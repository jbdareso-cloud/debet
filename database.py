"""Data layer for DeBet: users, settings, tips and bets.

Runs on a local SQLite file by default (debet.db). When the DATABASE_URL environment variable (or
Streamlit secret) is set to a Postgres address - e.g. a Supabase "Session pooler" connection string -
it uses that online database instead. The SQL below is written once; _pg_sql() translates the few
SQLite-only bits for Postgres.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import ssl
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

DB_PATH = os.environ.get(
    "DEBET_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "debet.db")
)


def database_url():
    """The online (Postgres) database address, or '' to use the local SQLite file."""
    return (os.environ.get("DATABASE_URL") or "").strip()


def using_postgres():
    return database_url().startswith(("postgres://", "postgresql://"))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ROLES = ("admin", "subscriber")
TRIAL_TIER = "Trial All-Access"
EXPIRED_TIER = "Expired"
PAID_TIERS = ("Silver", "Gold", "Platinum VVIP")  # lowest -> highest
SUBSCRIPTION_TIERS = (TRIAL_TIER, *PAID_TIERS, EXPIRED_TIER)
TIP_TIERS = ("Unassigned", *PAID_TIERS)
AI_VERDICTS = ("PLAY AS IS", "PLAY REDUCED STAKE", "AVOID")
RESULTS = ("PENDING", "WON", "LOST", "VOID")
TIP_GROUPS = ("Group A - First 4", "Group B - Next 3")
FINAL_TIP_CHOICES = ("original", "alternative")  # which market a published tip actually uses
APPROVED_VERDICTS = ("PLAY AS IS", "PLAY REDUCED STAKE")
PERFORMANCE_WINDOW_DAYS = 30
BET_TYPES = {1: "Single", 2: "Double", 3: "Triple", 4: "4-Fold Accumulator"}
BANKROLL_PERIODS = ((7, "Weekly (7 Days)"), (30, "Monthly (30 Days)"), (365, "Annual (365 Days)"))
TRIAL_DAYS = 30
SUBSCRIPTION_DAYS = 30

# Auto tier from Claude Opus's final confidence (1-10). Checked top-down; below 5.5 -> Unassigned.
AUTO_TIER_THRESHOLDS = ((8.5, "Platinum VVIP"), (7.0, "Gold"), (5.5, "Silver"))


def recommended_tier(confidence, verdict=None):
    """Map a confidence score to the tier a tip should be published in.

    8.5-10 -> Platinum VVIP, 7.0-8.4 -> Gold, 5.5-6.9 -> Silver, below 5.5 -> Unassigned.
    An AVOID verdict is never auto-tiered, whatever the score.
    """
    if confidence is None or verdict == "AVOID":
        return "Unassigned"
    for threshold, tier in AUTO_TIER_THRESHOLDS:
        if float(confidence) >= threshold:
            return tier
    return "Unassigned"

DEFAULT_SETTINGS = {
    "ADMIN_WHATSAPP": "+000000000000",
    "ADMIN_PHONE": "+000000000000",
}
DEFAULT_ADMIN_EMAIL = "admin@debet.local"
DEFAULT_ADMIN_PHONE = "0000000000"


# ---------------------------------------------------------------------------
# Connection / time helpers
# ---------------------------------------------------------------------------
@contextmanager
def get_connection():
    """A connection for one unit of work: commits on success, rolls back on error."""
    if using_postgres():
        conn = _pg_session()
        try:
            yield conn
            conn.commit()
        except Exception as exc:
            conn.rollback()
            if not isinstance(exc, ValueError):  # possibly a dropped connection: start fresh next time
                _PG_LOCAL.conn = None
            raise
        return
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def table_columns(conn, table):
    """Column names of a table, on either database."""
    if using_postgres():
        return {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?", (table,))}
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def is_unique_violation(exc):
    return isinstance(exc, sqlite3.IntegrityError) or "23505" in str(exc) or "unique" in str(exc).lower()


# --- Postgres (Supabase) support ---------------------------------------------------------------
_PG_LOCAL = threading.local()
_PG_IDLE_PING = 60          # seconds; re-check a connection that has been idle longer than this
_PG_RETURNING_ID = ("users", "tips", "user_bets", "jobs")


def _pg_sql(sql):
    """Translate this module's SQLite-flavoured SQL to Postgres."""
    s = sql.replace("%", "%%")
    s = s.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    s = s.replace("BOOLEAN NOT NULL DEFAULT 0", "INTEGER NOT NULL DEFAULT 0")
    s = s.replace("TEXT UNIQUE COLLATE NOCASE", "TEXT UNIQUE")
    s = re.sub(r"\bREAL\b", "DOUBLE PRECISION", s)
    s = re.sub(r"(\w+) = \? COLLATE NOCASE", r"LOWER(\1) = LOWER(?)", s)
    m = re.match(r"\s*INSERT OR IGNORE INTO (.*)$", s, re.DOTALL)
    if m:
        s = f"INSERT INTO {m.group(1).rstrip().rstrip(';')} ON CONFLICT DO NOTHING"
    return s.replace("?", "%s")


class _PgCursor:
    def __init__(self, rows, rowcount, lastrowid=None):
        self._rows, self.rowcount, self.lastrowid = rows, rowcount, lastrowid

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class _PgConn:
    """Makes a pg8000 connection look like the sqlite3 connection this module was written for:
    '?' placeholders, dict rows, cursor.lastrowid, executescript. Writes run in a transaction that
    starts on the first write; reads run on their own (autocommit) to avoid extra round trips."""

    def __init__(self, raw):
        self.raw, self.in_tx, self.last_used = raw, False, time.monotonic()

    def _run(self, sql, params=()):
        cur = self.raw.cursor()
        cur.execute(sql, tuple(params) if params else ())
        rows = []
        if cur.description:
            names = [d[0] for d in cur.description]
            rows = [dict(zip(names, r)) for r in cur.fetchall()]
        return cur, rows

    def execute(self, sql, params=()):
        self.last_used = time.monotonic()
        s = _pg_sql(sql)
        head = s.lstrip().split(None, 1)[0].upper() if s.strip() else ""
        if head not in ("SELECT", "WITH") and not self.in_tx:
            self._run("BEGIN")
            self.in_tx = True
        insert = re.match(r"\s*INSERT INTO (\w+)", s, re.IGNORECASE)
        if insert and insert.group(1) in _PG_RETURNING_ID and "RETURNING" not in s.upper():
            s += " RETURNING id"
            cur, rows = self._run(s, params)
            return _PgCursor(rows, cur.rowcount, rows[0]["id"] if rows else None)
        cur, rows = self._run(s, params)
        return _PgCursor(rows, cur.rowcount)

    def executemany(self, sql, seq):
        for params in seq:
            self.execute(sql, params)

    def executescript(self, script):
        for stmt in re.sub(r"--[^\n]*", "", script).split(";"):
            if stmt.strip():
                self.execute(stmt)

    def commit(self):
        if self.in_tx:
            self._run("COMMIT")
            self.in_tx = False

    def rollback(self):
        if self.in_tx:
            try:
                self._run("ROLLBACK")
            except Exception:
                _PG_LOCAL.conn = None  # broken connection: reconnect next time
            self.in_tx = False


_DB_URL_RE = re.compile(r"^postgres(?:ql)?://(?P<user>[^:/@]+):(?P<password>.*)@(?P<host>[^@/:]+)"
                        r"(?::(?P<port>\d+))?(?:/(?P<db>[^?#]*))?(?:\?.*)?$", re.DOTALL)


def _parse_db_url(url):
    """Split a postgres address into its parts. The password may contain symbols such as # @ / that
    would break a normal URL parser. Errors never include the address (it holds the password)."""
    m = _DB_URL_RE.match(url.strip())
    if not m:
        raise ValueError("DATABASE_URL isn't a valid postgres address (expected "
                         "postgresql://user:password@host:5432/postgres).")
    return {"user": unquote(m["user"]), "password": m["password"], "host": m["host"],
            "port": int(m["port"] or 5432), "database": m["db"] or "postgres"}


def _pg_connect():
    import pg8000.dbapi  # imported only when an online database is configured
    p = _parse_db_url(database_url())
    # Supabase signs its database certificate with its own CA. With DATABASE_SSL_ROOT_CERT pointing at that
    # CA file (Supabase dashboard -> Database settings -> SSL certificate) the server is fully verified;
    # otherwise the connection is encrypted without verifying the CA, like Supabase's default sslmode=require.
    root = (os.environ.get("DATABASE_SSL_ROOT_CERT") or "").strip()
    if root:
        ctx = ssl.create_default_context(cafile=root)
    else:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        raw = pg8000.dbapi.connect(user=p["user"], password=p["password"], host=p["host"], port=p["port"],
                                   database=p["database"], ssl_context=ctx, timeout=30)
    except Exception as exc:
        msg = str(exc).replace(p["password"], "********") if p["password"] else str(exc)
        raise RuntimeError(f"Couldn't connect to the online database: {msg}") from None
    raw.autocommit = True
    return _PgConn(raw)


def _pg_session():
    """One reusable connection per thread (opening a new TLS connection per query would be slow)."""
    conn = getattr(_PG_LOCAL, "conn", None)
    if conn is not None and time.monotonic() - conn.last_used > _PG_IDLE_PING:
        try:
            conn._run("SELECT 1")
        except Exception:
            conn = None
    if conn is None:
        conn = _pg_connect()
        _PG_LOCAL.conn = conn
    conn.last_used = time.monotonic()
    return conn


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _parse(value):
    return datetime.fromisoformat(value) if value else None


def _row(row):
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Password hashing (PBKDF2-SHA256, salted)
# ---------------------------------------------------------------------------
_PBKDF2_ITERATIONS = 260_000


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password, stored_hash):
    try:
        _, iterations, salt, digest_hex = stored_hash.split("$")
    except (ValueError, AttributeError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations))
    return hmac.compare_digest(digest.hex(), digest_hex)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS users (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name           TEXT NOT NULL,
    email               TEXT UNIQUE COLLATE NOCASE,          -- optional (NULL for phone-only sign-ups)
    phone_number        TEXT NOT NULL UNIQUE,                -- login identifier, one account per number
    password_hash       TEXT NOT NULL,
    role                TEXT NOT NULL DEFAULT 'subscriber'
                        CHECK (role IN {ROLES}),
    subscription_tier   TEXT NOT NULL DEFAULT '{TRIAL_TIER}'
                        CHECK (subscription_tier IN {SUBSCRIPTION_TIERS}),
    trial_start_date    TEXT,
    subscription_expiry TEXT,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tips (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at         TEXT NOT NULL,
    match_date         TEXT,
    match_name         TEXT NOT NULL,
    competition        TEXT,
    user_proposed_tip  TEXT NOT NULL,
    odds               REAL,
    tier               TEXT NOT NULL DEFAULT 'Unassigned'
                       CHECK (tier IN {TIP_TIERS}),
    ai_verdict       TEXT CHECK (ai_verdict IS NULL OR ai_verdict IN {AI_VERDICTS}),
    confidence_score   REAL,
    edge_summary       TEXT,
    risk_factor        TEXT,
    alternative_market TEXT,
    full_debate_log    TEXT,
    final_score        TEXT NOT NULL DEFAULT '',
    result             TEXT NOT NULL DEFAULT 'PENDING'
                       CHECK (result IN {RESULTS}),
    is_published       BOOLEAN NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_tips_tier_published ON tips (tier, is_published);
"""

# Personal bet slips. Created in _migrate() AFTER the users-table rebuild, and without a foreign key,
# so that rebuild (DROP TABLE users) can never cascade into anyone's slips.
# `legs` locks each leg's market, odds and which result column settles it at the moment the slip
# was logged, so an admin later switching a tip to its AI alternative doesn't rewrite old slips.
_USER_BETS_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS user_bets (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            INTEGER NOT NULL,
    created_at         TEXT NOT NULL,
    bet_type           TEXT NOT NULL CHECK (bet_type IN {tuple(BET_TYPES.values())}),
    tip_ids            TEXT NOT NULL,                -- JSON list of tip ids
    legs               TEXT NOT NULL DEFAULT '[]',   -- JSON [{{tip_id, use, market, odds}}]
    selections_summary TEXT NOT NULL,
    total_odds         REAL NOT NULL,
    stake_amount       REAL NOT NULL CHECK (stake_amount > 0),
    potential_payout   REAL NOT NULL,
    status             TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN {RESULTS}),
    net_profit         REAL                          -- NULL while PENDING
);
CREATE INDEX IF NOT EXISTS idx_user_bets_user ON user_bets (user_id, created_at);
"""

# Columns added after the first release: (name, definition). Added by _migrate() when missing,
# so existing databases keep every record.
_TIP_COLUMNS_V2 = (
    ("auto_tier", f"TEXT NOT NULL DEFAULT 'Unassigned' CHECK (auto_tier IN {TIP_TIERS})"),
    ("tip_group", f"TEXT CHECK (tip_group IS NULL OR tip_group IN {TIP_GROUPS})"),
    ("alt_market", "TEXT"),          # clean Part B market, e.g. 'Over 1.5 Goals'
    ("alt_odds", "REAL"),            # approximate Part B odds
    ("alt_result", f"TEXT NOT NULL DEFAULT 'PENDING' CHECK (alt_result IN {RESULTS})"),
    ("final_tip_used", f"TEXT NOT NULL DEFAULT 'original' CHECK (final_tip_used IN {FINAL_TIP_CHOICES})"),
    ("slot_number", "INTEGER CHECK (slot_number IS NULL OR slot_number BETWEEN 1 AND 7)"),  # studio slot 1-7
    # alt_odds holds the REAL bookmaker price the admin enters for Part B; the AI's own guess at a
    # fair price is kept separately here and is never used for payouts, slips or ROI.
    ("alt_fair_odds", "REAL"),
    ("published_at", "TEXT"),        # first time the tip went live; once set (after a grace period) it's locked
    # Value engine (Grok + Claude estimate independently; value is computed in code from the odds)
    ("p_grok", "REAL"),              # Grok's probability that the tip's market lands
    ("p_claude", "REAL"),            # Claude's probability
    ("p_final", "REAL"),             # combined probability (after the challenge round, if any)
    ("value", "REAL"),               # p_final * odds - 1
    ("model_gap", "REAL"),           # |p_grok - p_claude|
    ("probs_json", "TEXT"),          # both models' full market estimates, reasons and missing info
    ("challenge_note", "TEXT"),      # result of the challenge round on the day's best candidate
    ("decision", "TEXT"),            # BEST / VALUE / NO VALUE / SPLIT / SHORT PRICE / NO ODDS
    ("closing_odds", "REAL"),        # the tip's market price at kickoff (entered by you), for closing-line value
    ("ai_cost", "REAL"),             # what analysing this tip cost in USD
)

# Group A slots carry the tipster's 4 tips, Group B slots the user's own picks.
SOURCE_LABELS = {TIP_GROUPS[0]: "Daily", TIP_GROUPS[1]: "Mine"}

# A published tip can be changed or unpublished for this long (to fix a mis-click); after that it is
# locked so the public track record can't be edited.
PUBLISH_GRACE_MINUTES = 15
SESSION_DAYS = 30

_SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id);
CREATE TABLE IF NOT EXISTS login_failures (
    ident  TEXT NOT NULL,
    at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_failures ON login_failures (ident, at);
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL,                -- running / done / failed
    total       INTEGER NOT NULL,
    done        INTEGER NOT NULL DEFAULT 0,
    message     TEXT,
    tip_ids     TEXT NOT NULL DEFAULT '[]',
    cost_usd    REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ai_costs (
    at     TEXT NOT NULL,
    label  TEXT NOT NULL,
    usd    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_costs_at ON ai_costs (at);
CREATE TABLE IF NOT EXISTS client_days (
    day           TEXT PRIMARY KEY,           -- match day, YYYY-MM-DD
    data          TEXT NOT NULL,              -- JSON from client_tiers.build_day
    published_at  TEXT                        -- NULL = draft, only the admin sees it
);
"""

JOB_STALE_MINUTES = 20      # a "running" analysis older than this was interrupted (e.g. the server restarted)
DUPLICATE_DAYS = 3          # the same match + market analysed within this many days counts as a duplicate
DEFAULT_STAKE_PCT = 2.0

LOGIN_MAX_FAILURES = 5      # wrong passwords allowed per login name...
LOGIN_LOCK_MINUTES = 15     # ...within this window, before that login is locked for the same time

# Studio slots: Group A = slots 1-4, Group B = slots 5-7.
GROUP_SLOTS = {TIP_GROUPS[0]: range(1, 5), TIP_GROUPS[1]: range(5, 8)}

# Result of the market a published tip actually went out with.
_EFFECTIVE_RESULT = "(CASE WHEN final_tip_used = 'alternative' THEN alt_result ELSE result END)"


def init_db(seed_admin=True):
    """Create tables, seed default settings and (unless seed_admin=False) the default admin account.

    Returns the new admin's credentials dict if one was just created, else None.
    """
    with get_connection() as conn:
        conn.executescript(_SCHEMA)
        conn.executemany(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            DEFAULT_SETTINGS.items(),
        )
        _migrate(conn)
    return _seed_default_admin() if seed_admin else None


def _migrate_users_email_optional(conn):
    """Older databases declared users.email NOT NULL. SQLite can't drop that constraint in place,
    so rebuild the table with the current schema and copy every row across (ids kept)."""
    if using_postgres():
        return  # online databases are always created with the current schema
    email_col = next((r for r in conn.execute("PRAGMA table_info(users)") if r["name"] == "email"), None)
    if not email_col or not email_col["notnull"]:
        return
    new_table = _SCHEMA.split("CREATE TABLE IF NOT EXISTS users", 1)[1].split(";", 1)[0]
    cols = ("id, full_name, email, phone_number, password_hash, role, subscription_tier, "
            "trial_start_date, subscription_expiry, created_at")
    conn.execute("DROP TABLE IF EXISTS users_new")
    conn.execute(f"CREATE TABLE users_new {new_table}")
    conn.execute(f"INSERT INTO users_new ({cols}) SELECT {cols} FROM users")
    conn.execute("UPDATE users_new SET email = NULL WHERE TRIM(email) = ''")
    conn.execute("DROP TABLE users")
    conn.execute("ALTER TABLE users_new RENAME TO users")


def _migrate(conn):
    """Bring databases created by older versions up to date (safe to run on every startup)."""
    _migrate_users_email_optional(conn)
    conn.executescript(_USER_BETS_SCHEMA)
    conn.executescript(_SESSIONS_SCHEMA)
    cols = table_columns(conn, "tips")
    added = [name for name, _ in _TIP_COLUMNS_V2 if name not in cols]
    for name, definition in _TIP_COLUMNS_V2:
        if name not in cols:
            conn.execute(f"ALTER TABLE tips ADD COLUMN {name} {definition}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tips_created ON tips (created_at)")

    if "alt_fair_odds" in added:
        # Every alt_odds stored so far was the AI's estimate, not a bookmaker price: move it aside.
        # Published alternatives need their real odds re-entered before they count in ROI or slips.
        conn.execute("UPDATE tips SET alt_fair_odds = alt_odds, alt_odds = NULL WHERE alt_odds IS NOT NULL")
    if "published_at" in added:
        conn.execute("UPDATE tips SET published_at = created_at WHERE is_published = 1")

    if "auto_tier" in added:
        for row in conn.execute("SELECT id, confidence_score, ai_verdict FROM tips").fetchall():
            conn.execute("UPDATE tips SET auto_tier = ? WHERE id = ?",
                         (recommended_tier(row["confidence_score"], row["ai_verdict"]), row["id"]))

    # Older parser stored the whole input line as both match and market (and lost the odds).
    broken = conn.execute("SELECT id, match_name FROM tips WHERE match_name = user_proposed_tip").fetchall()
    if broken:
        from debate_engine import parse_tip  # lazy import keeps startup light
        for row in broken:
            try:
                tip = parse_tip(row["match_name"])
            except ValueError:
                continue
            conn.execute("UPDATE tips SET match_name = ?, user_proposed_tip = ?, odds = COALESCE(odds, ?) "
                         "WHERE id = ?", (tip["match_name"], tip["market"], tip["odds"], row["id"]))


def _seed_default_admin():
    with get_connection() as conn:
        if conn.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone():
            return None
        password = secrets.token_urlsafe(12)
        conn.execute(
            """INSERT INTO users (full_name, email, phone_number, password_hash, role,
                                  subscription_tier, trial_start_date, subscription_expiry, created_at)
               VALUES (?, ?, ?, ?, 'admin', 'Platinum VVIP', NULL, NULL, ?)""",
            ("Default Admin", DEFAULT_ADMIN_EMAIL, DEFAULT_ADMIN_PHONE,
             hash_password(password), _iso(_now())),
        )
    print("=" * 60)
    print("Default admin account created (shown only once):")
    print(f"  Email:    {DEFAULT_ADMIN_EMAIL}")
    print(f"  Phone:    {DEFAULT_ADMIN_PHONE}")
    print(f"  Password: {password}")
    print("Log in and change this password as soon as possible.")
    print("=" * 60)
    return {"email": DEFAULT_ADMIN_EMAIL, "phone_number": DEFAULT_ADMIN_PHONE, "password": password}


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
_PUBLIC_USER_COLS = ("id, full_name, email, phone_number, role, subscription_tier, "
                     "trial_start_date, subscription_expiry, created_at")


def normalize_phone(phone_number):
    """'+256 700-123 456' -> '+256700123456': keep a leading '+' and digits only, so the same
    number typed with different spacing can't create two accounts."""
    raw = (phone_number or "").strip()
    digits = re.sub(r"\D", "", raw)
    return ("+" + digits) if raw.startswith("+") and digits else digits


def register_user(full_name, phone_number, password, email=None):
    """Register instantly and start a 30-day Trial All-Access. Returns the user dict.

    Only name, phone number and password are required; email is optional (stored as NULL).
    Raises ValueError if fields are missing or the phone number (or email) is already taken.
    """
    full_name, phone_number = (full_name or "").strip(), normalize_phone(phone_number)
    email = (email or "").strip().lower() or None
    if not (full_name and phone_number and password):
        raise ValueError("Full name, phone number and password are required.")
    now = _now()
    try:
        with get_connection() as conn:
            cur = conn.execute(
                """INSERT INTO users (full_name, email, phone_number, password_hash, role,
                                      subscription_tier, trial_start_date, subscription_expiry, created_at)
                   VALUES (?, ?, ?, ?, 'subscriber', ?, ?, ?, ?)""",
                (full_name, email, phone_number, hash_password(password), TRIAL_TIER,
                 _iso(now), _iso(now + timedelta(days=TRIAL_DAYS)), _iso(now)),
            )
            user_id = cur.lastrowid
    except Exception as exc:
        if not is_unique_violation(exc):
            raise
        raise ValueError("An account with that phone number already exists. Please log in instead.") from exc
    return get_user(user_id)


def login_user(phone_or_email, password):
    """Log in by phone number (subscribers) or email (e.g. the admin's admin@debet.local).

    Return the user dict (without password hash) on success, else None.
    """
    ident = (phone_or_email or "").strip()
    if not ident:
        return None
    with get_connection() as conn:
        if "@" in ident:
            row = conn.execute("SELECT * FROM users WHERE email = ? COLLATE NOCASE", (ident,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM users WHERE phone_number IN (?, ?)",
                               (normalize_phone(ident), ident)).fetchone()
    if not row or not verify_password(password, row["password_hash"]):
        return None
    user = dict(row)
    user.pop("password_hash")
    return user


def _login_key(ident):
    return (ident or "").strip().lower()


def login_lock_minutes_left(ident):
    """Minutes until this login name may try again (0 if not locked)."""
    since = _iso(_now() - timedelta(minutes=LOGIN_LOCK_MINUTES))
    with get_connection() as conn:
        rows = conn.execute("SELECT at FROM login_failures WHERE ident = ? AND at >= ? ORDER BY at",
                            (_login_key(ident), since)).fetchall()
    if len(rows) < LOGIN_MAX_FAILURES:
        return 0
    unlock = _parse(rows[-LOGIN_MAX_FAILURES]["at"]) + timedelta(minutes=LOGIN_LOCK_MINUTES)
    return max(1, int((unlock - _now()).total_seconds() // 60) + 1)


def record_login_failure(ident):
    with get_connection() as conn:
        conn.execute("DELETE FROM login_failures WHERE at < ?",
                     (_iso(_now() - timedelta(days=1)),))  # housekeeping
        conn.execute("INSERT INTO login_failures (ident, at) VALUES (?, ?)", (_login_key(ident), _iso(_now())))


def clear_login_failures(ident):
    with get_connection() as conn:
        conn.execute("DELETE FROM login_failures WHERE ident = ?", (_login_key(ident),))


def get_user(user_id):
    with get_connection() as conn:
        return _row(conn.execute(f"SELECT {_PUBLIC_USER_COLS} FROM users WHERE id = ?", (user_id,)).fetchone())


def change_password(user_id, new_password):
    if not new_password:
        raise ValueError("Password cannot be empty.")
    with get_connection() as conn:
        cur = conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                           (hash_password(new_password), user_id))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))  # log out other devices
    return cur.rowcount == 1


def reset_admin_password():
    """Server-console recovery: give the first admin a new random password and print it.
    Run `python database.py --reset-admin-password` on the server."""
    with get_connection() as conn:
        row = conn.execute("SELECT id, email, phone_number FROM users WHERE role = 'admin' "
                           "ORDER BY id LIMIT 1").fetchone()
    if not row:
        raise ValueError("No admin account exists yet - start the app once to create it.")
    password = secrets.token_urlsafe(12)
    change_password(row["id"], password)
    return {"email": row["email"], "phone_number": row["phone_number"], "password": password}


# ---------------------------------------------------------------------------
# Login sessions (remember-me cookie). Only a SHA-256 of the token is stored.
# ---------------------------------------------------------------------------
def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id, days=SESSION_DAYS):
    """Start a login session and return its secret token (to put in the browser cookie)."""
    token = secrets.token_urlsafe(32)
    now = _now()
    with get_connection() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_iso(now),))  # housekeeping
        conn.execute("INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                     (_token_hash(token), user_id, _iso(now), _iso(now + timedelta(days=days))))
    return token


def get_session_user(token):
    """The user a valid, unexpired session token belongs to, else None."""
    if not token:
        return None
    with get_connection() as conn:
        row = conn.execute("SELECT user_id, expires_at FROM sessions WHERE token_hash = ?",
                           (_token_hash(token),)).fetchone()
    if not row or _parse(row["expires_at"]) <= _now():
        return None
    return get_user(row["user_id"])


def delete_session(token):
    if token:
        with get_connection() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


# --- Admin user management -------------------------------------------------
def list_users(role=None):
    query = f"SELECT {_PUBLIC_USER_COLS} FROM users"
    params = ()
    if role:
        query += " WHERE role = ?"
        params = (role,)
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(query + " ORDER BY created_at DESC, id DESC", params)]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def get_setting(key, default=None):
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def get_all_settings():
    with get_connection() as conn:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def update_setting(key, value):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


# ---------------------------------------------------------------------------
# Tips
# ---------------------------------------------------------------------------
def save_tip(match_name, user_proposed_tip, match_date=None, competition=None, odds=None,
             ai_verdict=None, confidence_score=None, edge_summary=None, risk_factor=None,
             alternative_market=None, full_debate_log=None, tier="Unassigned", is_published=False,
             auto_tier=None, tip_group=None, alt_market=None, alt_odds=None, slot_number=None,
             alt_fair_odds=None, **value_fields):
    """Insert a new tip (result PENDING). Returns the new tip dict.

    `value_fields` may carry the value engine's columns (p_grok, p_claude, p_final, value,
    model_gap, probs_json, challenge_note, decision).

    Every submitted tip is saved, including AVOIDs and failed analyses (ai_verdict None), so raw
    performance can be tracked. `user_proposed_tip` is the market (e.g. 'Over 2.5 Goals');
    The alt_*/tier/publish arguments belong to the old subscriber version and are kept only so older
    rows still load; the app no longer sets them.
    """
    if auto_tier is None:
        auto_tier = recommended_tier(confidence_score, ai_verdict)
    for name, value in (("Tier", tier), ("Auto tier", auto_tier)):
        if value not in TIP_TIERS:
            raise ValueError(f"{name} must be one of {TIP_TIERS}.")
    if ai_verdict is not None and ai_verdict not in AI_VERDICTS:
        raise ValueError(f"ai_verdict must be one of {AI_VERDICTS}.")
    if tip_group is not None and tip_group not in TIP_GROUPS:
        raise ValueError(f"tip_group must be one of {TIP_GROUPS}.")
    if slot_number is not None:
        slot_number = int(slot_number)
        if tip_group and slot_number not in GROUP_SLOTS[tip_group]:
            raise ValueError(f"Slot {slot_number} doesn't belong to {tip_group}.")
        if not 1 <= slot_number <= 7:
            raise ValueError("slot_number must be 1-7.")
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO tips (created_at, match_date, match_name, competition, user_proposed_tip,
                                 odds, tier, auto_tier, ai_verdict, confidence_score, edge_summary,
                                 risk_factor, alternative_market, full_debate_log, is_published,
                                 tip_group, alt_market, alt_odds, slot_number, alt_fair_odds, published_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (_iso(_now()), match_date, match_name, competition, user_proposed_tip, odds, tier,
             auto_tier, ai_verdict, confidence_score, edge_summary, risk_factor, alternative_market,
             full_debate_log, int(bool(is_published)), tip_group, alt_market, alt_odds, slot_number,
             alt_fair_odds, _iso(_now()) if is_published else None),
        )
        tip_id = cur.lastrowid
        extra = {k: v for k, v in value_fields.items() if k in _VALUE_COLUMNS}
        unknown = set(value_fields) - _VALUE_COLUMNS
        if unknown:
            raise ValueError(f"Unknown tip fields: {sorted(unknown)}")
        if extra:
            conn.execute(f"UPDATE tips SET {', '.join(f'{k} = ?' for k in extra)} WHERE id = ?",
                         (*extra.values(), tip_id))
    return get_tip(tip_id)


_VALUE_COLUMNS = {"p_grok", "p_claude", "p_final", "value", "model_gap", "probs_json",
                  "challenge_note", "decision", "ai_cost"}
_UPDATABLE_TIP_COLUMNS = _VALUE_COLUMNS | {"ai_verdict", "full_debate_log", "risk_factor", "edge_summary"}


def update_tip_fields(tip_id, **fields):
    """Update value-engine fields of an existing tip (e.g. after the day's challenge round)."""
    bad = set(fields) - _UPDATABLE_TIP_COLUMNS
    if bad:
        raise ValueError(f"Can't update {sorted(bad)}")
    if fields:
        with get_connection() as conn:
            conn.execute(f"UPDATE tips SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                         (*fields.values(), tip_id))
    return get_tip(tip_id)


def set_closing_odds(tip_id, odds):
    """Record the tip's market price at kickoff (None clears it)."""
    if odds is not None:
        odds = round(float(odds), 2)
        if not 1.0 < odds <= 1000:
            raise ValueError("Closing odds must be decimal odds greater than 1.00.")
    with get_connection() as conn:
        conn.execute("UPDATE tips SET closing_odds = ? WHERE id = ?", (odds, tip_id))


def find_duplicates(pairs, days=DUPLICATE_DAYS):
    """pairs: [(match_name, market)]. Returns the set of (match, market) (lower-cased) already analysed
    in the last `days` days (failed analyses don't count)."""
    since = _iso(_now() - timedelta(days=days))
    with get_connection() as conn:
        rows = conn.execute("SELECT match_name, user_proposed_tip FROM tips WHERE created_at >= ? "
                            "AND ai_verdict IS NOT NULL", (since,)).fetchall()
    seen = {(r["match_name"].strip().lower(), r["user_proposed_tip"].strip().lower()) for r in rows}
    return {(m.strip().lower(), k.strip().lower()) for m, k in pairs} & seen


# --- Background analysis jobs ---------------------------------------------------------------------
def create_job(total):
    with get_connection() as conn:
        cur = conn.execute("INSERT INTO jobs (created_at, status, total, done, tip_ids, cost_usd) "
                           "VALUES (?, 'running', ?, 0, '[]', 0)", (_iso(_now()), total))
        return cur.lastrowid


def update_job(job_id, **fields):
    allowed = {"status", "done", "message", "tip_ids", "cost_usd"}
    fields = {k: (json.dumps(v) if k == "tip_ids" else v) for k, v in fields.items() if k in allowed}
    with get_connection() as conn:
        conn.execute(f"UPDATE jobs SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                     (*fields.values(), job_id))


def _job(row):
    if not row:
        return None
    job = dict(row)
    job["tip_ids"] = json.loads(job.get("tip_ids") or "[]")
    if job["status"] == "running" and _now() - _parse(job["created_at"]) > timedelta(minutes=JOB_STALE_MINUTES):
        job["status"] = "interrupted"
    return job


def get_job(job_id):
    with get_connection() as conn:
        return _job(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())


def latest_job(hours=24):
    """The most recent analysis run started in the last `hours` hours, or None."""
    since = _iso(_now() - timedelta(hours=hours))
    with get_connection() as conn:
        return _job(conn.execute("SELECT * FROM jobs WHERE created_at >= ? ORDER BY id DESC LIMIT 1",
                                 (since,)).fetchone())


# --- AI spend ------------------------------------------------------------------------------------
def record_ai_cost(label, usd):
    if usd:
        with get_connection() as conn:
            conn.execute("INSERT INTO ai_costs (at, label, usd) VALUES (?, ?, ?)", (_iso(_now()), label, float(usd)))


def ai_spend(days=None, since_month_start=False):
    """Total AI spend in USD: this calendar month (UTC) or the last `days` days."""
    now = _now()
    since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) if since_month_start \
        else now - timedelta(days=days or 30)
    with get_connection() as conn:
        row = conn.execute("SELECT COALESCE(SUM(usd), 0) AS total, COUNT(*) AS n FROM ai_costs WHERE at >= ?",
                           (_iso(since),)).fetchone()
    return round(float(row["total"] or 0), 4), row["n"]


# --- Bankroll & stakes -----------------------------------------------------------------------------
def bankroll_settings():
    try:
        start = float(get_setting("BANKROLL_START") or 0)
    except ValueError:
        start = 0.0
    try:
        pct = float(get_setting("STAKE_PCT") or DEFAULT_STAKE_PCT)
    except ValueError:
        pct = DEFAULT_STAKE_PCT
    return start, pct


def save_bankroll_settings(start, pct):
    if start < 0 or not 0.1 <= pct <= 10:
        raise ValueError("Bankroll must be 0 or more and the stake between 0.1% and 10%.")
    update_setting("BANKROLL_START", round(float(start), 2))
    update_setting("STAKE_PCT", round(float(pct), 2))


def current_bankroll(user_id):
    """Starting bankroll plus all settled profit/loss from logged bets (None if no bankroll set)."""
    start, _ = bankroll_settings()
    if not start:
        return None
    with get_connection() as conn:
        row = conn.execute("SELECT COALESCE(SUM(net_profit), 0) AS net FROM user_bets WHERE user_id = ? "
                           "AND status != 'PENDING'", (user_id,)).fetchone()
    return round(start + float(row["net"] or 0), 2)


def suggested_stake(user_id):
    """Flat stake = STAKE_PCT of the current bankroll, rounded to the nearest 500. None without a bankroll."""
    bank = current_bankroll(user_id)
    if not bank or bank <= 0:
        return None
    _, pct = bankroll_settings()
    return max(500, int(round(bank * pct / 100 / 500.0)) * 500)


# --- Closing-line value ------------------------------------------------------------------------------
def get_clv_stats(user_id, days=PERFORMANCE_WINDOW_DAYS):
    """Average 'price vs kickoff price' (odds / closing_odds - 1) for tips and for your logged bet legs.
    Positive means you got better odds than the market closed at - the best early sign of a real edge."""
    since = _iso(_now() - timedelta(days=days))
    with get_connection() as conn:
        tips = {r["id"]: dict(r) for r in conn.execute(
            "SELECT id, odds, closing_odds, tip_group FROM tips WHERE closing_odds IS NOT NULL AND created_at >= ?",
            (since,))}
        bets = [dict(r) for r in conn.execute("SELECT legs FROM user_bets WHERE user_id = ? AND created_at >= ?",
                                              (user_id, since))]

    def avg(values):
        return (round(100 * sum(values) / len(values), 1), len(values)) if values else (None, 0)

    tipster = [t["odds"] / t["closing_odds"] - 1 for t in tips.values() if t["odds"] and t["tip_group"] == TIP_GROUPS[0]]
    mine_tips = [t["odds"] / t["closing_odds"] - 1 for t in tips.values() if t["odds"] and t["tip_group"] == TIP_GROUPS[1]]
    legs = [leg["odds"] / tips[leg["tip_id"]]["closing_odds"] - 1
            for b in bets for leg in json.loads(b["legs"] or "[]") if leg.get("tip_id") in tips]
    return {"tipster": avg(tipster), "mine": avg(mine_tips), "my_bets": avg(legs)}


# --- Backup ------------------------------------------------------------------------------------------
# --- Client tiers (Platinum / Gold / Silver + accumulator) -----------------------------------------
def save_client_day(day, data, publish):
    """Store a day's tiers; publish=True makes them visible to clients (False keeps/sets a draft)."""
    with get_connection() as conn:
        conn.execute("DELETE FROM client_days WHERE day = ?", (day,))
        conn.execute("INSERT INTO client_days (day, data, published_at) VALUES (?, ?, ?)",
                     (day, json.dumps(data), _iso(_now()) if publish else None))


def get_client_day(day):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM client_days WHERE day = ?", (day,)).fetchone()
    return {**json.loads(row["data"]), "published_at": row["published_at"]} if row else None


def list_published_days(limit=30):
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM client_days WHERE published_at IS NOT NULL ORDER BY day DESC LIMIT ?",
                            (limit,)).fetchall()
    return [{**json.loads(r["data"]), "published_at": r["published_at"]} for r in rows]


def delete_client(user_id):
    """Remove a client account (never an admin) and log it out everywhere."""
    with get_connection() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        return conn.execute("DELETE FROM users WHERE id = ? AND role = 'subscriber'", (user_id,)).rowcount == 1


def export_backup_zip():
    """A .zip (bytes) with tips, bets, settings and AI costs as CSV files. Passwords are not included."""
    import csv
    import io
    import zipfile
    buf = io.BytesIO()
    with get_connection() as conn, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for table in ("tips", "user_bets", "settings", "ai_costs", "jobs"):
            rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
            text = io.StringIO()
            if rows:
                writer = csv.DictWriter(text, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            zf.writestr(f"{table}.csv", text.getvalue())
        zf.writestr("README.txt", f"DeBet backup made {_iso(_now())} UTC.\nOpen the CSV files in Excel or Google Sheets.\n")
    return buf.getvalue()


def get_model_accuracy(days=None):
    """Brier score (lower = better) of Grok, Claude, the combined estimate and the bookmaker's
    implied probability (1/odds, margin not removed) on settled tips that have AI estimates."""
    query = "SELECT p_grok, p_claude, p_final, odds, result FROM tips WHERE result IN ('WON','LOST') " \
            "AND p_final IS NOT NULL"
    params = ()
    if days:
        query += " AND created_at >= ?"
        params = (_iso(_now() - timedelta(days=days)),)
    with get_connection() as conn:
        rows = [dict(r) for r in conn.execute(query, params)]

    def brier(key):
        pts = [((r[key] if key != "odds" else 1 / r["odds"]), 1.0 if r["result"] == "WON" else 0.0)
               for r in rows if r[key]]
        return (round(sum((p - y) ** 2 for p, y in pts) / len(pts), 4), len(pts)) if pts else (None, 0)

    return {"Grok": brier("p_grok"), "Claude": brier("p_claude"), "Combined": brier("p_final"),
            "Bookmaker odds": brier("odds"), "settled": len(rows)}


def get_tip(tip_id):
    with get_connection() as conn:
        return _row(conn.execute("SELECT * FROM tips WHERE id = ?", (tip_id,)).fetchone())


def list_tips(result=None, published=None, limit=None):
    """All tips (admin view), newest first, with optional filters."""
    clauses, params = [], []
    if result:
        clauses.append("result = ?")
        params.append(result)
    if published is not None:
        clauses.append("is_published = ?")
        params.append(int(bool(published)))
    query = "SELECT * FROM tips"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY created_at DESC, id DESC"
    if limit:
        query += " LIMIT ?"
        params.append(int(limit))
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(query, params)]


APP_TZ = timezone(timedelta(hours=3), "EAT")  # Uganda: the calendar day the admin and subscribers live in


def update_tip_result(tip_id, result, final_score="", alt_result=None):
    """Settle the original market (`result`) and, if given, the AI alternative (`alt_result`)."""
    if result not in RESULTS:
        raise ValueError(f"Result must be one of {RESULTS}.")
    if alt_result is not None and alt_result not in RESULTS:
        raise ValueError(f"alt_result must be one of {RESULTS}.")
    with get_connection() as conn:
        cur = conn.execute("UPDATE tips SET result = ?, final_score = ?, alt_result = COALESCE(?, alt_result) "
                           "WHERE id = ?", (result, final_score or "", alt_result, tip_id))
    if cur.rowcount == 0:
        raise ValueError(f"Tip {tip_id} not found.")
    recalculate_user_bets([tip_id])  # re-grade every slip that has this tip as a leg
    return get_tip(tip_id)


def list_tips_to_verify():
    """Every tip (published or not) whose original or alternative market is still unsettled.

    A pending alternative alone is only re-checked inside the performance window, so one the
    verifier can't settle doesn't trigger paid lookups forever.
    """
    since = _iso(_now() - timedelta(days=PERFORMANCE_WINDOW_DAYS))
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM tips WHERE result = 'PENDING' "
            "   OR (alt_market IS NOT NULL AND alt_result = 'PENDING' AND created_at >= ?) "
            "ORDER BY created_at, id", (since,))]


def delete_tip(tip_id):
    """Delete a draft. Tips that are (or were) published stay - they belong to the track record."""
    with get_connection() as conn:
        return conn.execute("DELETE FROM tips WHERE id = ? AND is_published = 0 AND published_at IS NULL",
                            (tip_id,)).rowcount == 1


def delete_failed_duplicates(match_name, market):
    """Drop earlier unpublished copies of this tip whose analysis failed (no verdict), so re-running
    a tip after an API error doesn't count it twice in raw performance."""
    with get_connection() as conn:
        return conn.execute(
            "DELETE FROM tips WHERE ai_verdict IS NULL AND is_published = 0 AND result = 'PENDING' "
            "AND match_name = ? COLLATE NOCASE AND user_proposed_tip = ? COLLATE NOCASE",
            (match_name, market)).rowcount


# --- Statistics --------------------------------------------------------------
# --- 30-day Performance Comparison Lab (admin only) ---------------------------------
PERFORMANCE_CATEGORIES = (
    ("group_a", "Daily tips - all"),
    ("group_b", "Mine - all picks"),
    ("raw_all", "All tips combined"),
    ("ai_approved", "AI value bets (every tip that passed)"),
    ("ai_best", "AI best pick of the day"),
)


def _summarize(picks):
    """picks: list of (result, odds). Win rate = WON / (WON + LOST); profit at 1 unit per bet."""
    won = sum(r == "WON" for r, _ in picks)
    lost = sum(r == "LOST" for r, _ in picks)
    settled = won + lost
    priced = [(r, o) for r, o in picks if r in ("WON", "LOST") and o]
    profit = sum((o - 1) if r == "WON" else -1 for r, o in priced)
    return {
        "total": len(picks), "won": won, "lost": lost,
        "void": sum(r == "VOID" for r, _ in picks),
        "pending": sum(r == "PENDING" for r, _ in picks),
        "settled": settled,
        "win_rate": round(100.0 * won / settled, 1) if settled else 0.0,
        "profit_units": round(profit, 2),
        "roi": round(100.0 * profit / len(priced), 1) if priced else None,
    }


def get_performance_lab(days=PERFORMANCE_WINDOW_DAYS):
    """Compare raw tips vs AI-filtered tips vs AI alternatives over the last `days` days.

    Covers ALL saved tips (published or not), by when they were submitted. Raw/approved categories
    are judged on the original market (`result`); the alternative category on `alt_result`.
    Returns {key: {"label": ..., **summary}} in PERFORMANCE_CATEGORIES order, plus "ungrouped":
    tips saved before groups existed (they count only in the combined category).
    """
    since = _iso(_now() - timedelta(days=days))
    with get_connection() as conn:
        tips = [dict(r) for r in conn.execute("SELECT * FROM tips WHERE created_at >= ?", (since,))]
    raw = [(t["result"], t["odds"]) for t in tips]
    picks = {
        "group_a": [(t["result"], t["odds"]) for t in tips if t["tip_group"] == TIP_GROUPS[0]],
        "group_b": [(t["result"], t["odds"]) for t in tips if t["tip_group"] == TIP_GROUPS[1]],
        "raw_all": raw,
        "ai_approved": [(t["result"], t["odds"]) for t in tips if t["ai_verdict"] in APPROVED_VERDICTS],
        "ai_best": [(t["result"], t["odds"]) for t in tips if t.get("decision") == "BEST"],
    }
    lab = {key: {"label": label, **_summarize(picks[key])} for key, label in PERFORMANCE_CATEGORIES}
    lab["ungrouped"] = sum(t["tip_group"] is None for t in tips)
    lab["since"] = since
    return lab


# ---------------------------------------------------------------------------
# Personal bet slips & bankroll (every user, admin included)
# ---------------------------------------------------------------------------
MAX_SLIP_LEGS = max(BET_TYPES)


def _money(value):
    return round(float(value), 2)


def bettable_selections(days=14):
    """Personal mode: every unsettled market you could still bet on, from tips saved in the last `days`
    days - your original market (if it has odds) and the AI alternative (once its real odds are entered).
    Returns [{"tip": tip, "use": "original"|"alternative", "market", "odds"}], soonest match first."""
    since = _iso(_now() - timedelta(days=days))
    with get_connection() as conn:
        tips = [dict(r) for r in conn.execute(
            "SELECT * FROM tips WHERE created_at >= ? AND (result = 'PENDING' OR alt_result = 'PENDING') "
            "ORDER BY match_date IS NULL, match_date, slot_number, id", (since,))]
    out = []
    for t in tips:
        if t["result"] == "PENDING" and t["odds"] and t["odds"] > 1:
            out.append({"tip": t, "use": "original", "market": t["user_proposed_tip"], "odds": t["odds"]})
        if t["alt_market"] and t["alt_result"] == "PENDING" and t["alt_odds"] and t["alt_odds"] > 1:
            out.append({"tip": t, "use": "alternative", "market": t["alt_market"], "odds": t["alt_odds"]})
    return out


def place_personal_bet(user_id, selections, stake_amount):
    """Personal mode: log a real slip of 1-4 legs, each (tip_id, "original"|"alternative"[, odds_you_got]).
    The optional third value is the price your bookmaker actually gave you; without it the tip's listed
    odds are used. Returns the saved slip dict; raises ValueError if invalid."""
    taken = {}
    pairs = []
    for s in selections or []:
        tid, use = int(s[0]), s[1]
        if len(s) > 2 and s[2]:
            taken[(tid, use)] = float(s[2])
        pairs.append((tid, use))
    sel = list(dict.fromkeys(pairs))
    if not 1 <= len(sel) <= MAX_SLIP_LEGS:
        raise ValueError(f"Pick between 1 and {MAX_SLIP_LEGS} selections.")
    try:
        stake = _money(stake_amount)
    except (TypeError, ValueError):
        raise ValueError("Enter your stake as a number.") from None
    if stake <= 0:
        raise ValueError("Stake must be greater than 0.")
    legs, matches = [], set()
    for tid, use in sel:
        tip = get_tip(tid)
        if not tip:
            raise ValueError(f"Selection #{tid} no longer exists.")
        alt = use == "alternative"
        market, odds = (tip["alt_market"], tip["alt_odds"]) if alt else (tip["user_proposed_tip"], tip["odds"])
        if (tid, use) in taken:
            odds = taken[(tid, use)]
            if not 1.0 < odds <= 1000:
                raise ValueError(f"{tip['match_name']}: odds must be greater than 1.00.")
        result = tip["alt_result"] if alt else tip["result"]
        if not market or result != "PENDING":
            raise ValueError(f"{tip['match_name']} is already settled - pick an upcoming match.")
        if not odds or odds <= 1:
            raise ValueError(f"{tip['match_name']}: {market} has no real odds yet.")
        key = tip["match_name"].strip().lower()
        if key in matches:
            raise ValueError(f"{tip['match_name']} appears twice - one selection per match.")
        matches.add(key)
        legs.append({"tip_id": tid, "use": "alternative" if alt else "original", "match": tip["match_name"],
                     "market": market, "odds": round(float(odds), 2)})
    total_odds = 1.0
    for leg in legs:
        total_odds *= leg["odds"]
    total_odds = round(total_odds, 2)
    summary = " + ".join(f"{l['match']}: {l['market']} @ {l['odds']:.2f}" for l in legs)
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO user_bets (user_id, created_at, bet_type, tip_ids, legs, selections_summary,
                                      total_odds, stake_amount, potential_payout, status, net_profit)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', NULL)""",
            (user_id, _iso(_now()), BET_TYPES[len(legs)], json.dumps([l["tip_id"] for l in legs]),
             json.dumps(legs), summary, total_odds, stake, _money(stake * total_odds)))
        bet_id = cur.lastrowid
    return get_user_bet(bet_id)


def get_user_bet(bet_id):
    with get_connection() as conn:
        return _row(conn.execute("SELECT * FROM user_bets WHERE id = ?", (bet_id,)).fetchone())


def list_user_bets(user_id, days=None):
    """A user's slips, newest first (optionally only those logged in the last `days` days)."""
    query, params = "SELECT * FROM user_bets WHERE user_id = ?", [user_id]
    if days:
        query += " AND created_at >= ?"
        params.append(_iso(_now() - timedelta(days=days)))
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(query + " ORDER BY created_at DESC, id DESC", params)]


def delete_user_bet(user_id, bet_id):
    """Remove one of the user's own PENDING slips (for mis-typed entries). Settled slips stay."""
    with get_connection() as conn:
        return conn.execute("DELETE FROM user_bets WHERE id = ? AND user_id = ? AND status = 'PENDING'",
                            (bet_id, user_id)).rowcount == 1


def _leg_result(leg, tips):
    """Result of one leg from the tips table. A leg whose tip was deleted counts as VOID (1.00)."""
    tip = tips.get(leg["tip_id"])
    if not tip:
        return "VOID"
    return tip["alt_result"] if leg.get("use") == "alternative" else tip["result"]


def grade_slip(stake, legs_with_results):
    """(status, net_profit) for a slip. legs_with_results: [(odds, result)].

    Any LOST leg -> LOST (-stake). Any PENDING leg -> PENDING (None). All VOID -> VOID (0).
    Otherwise WON, with VOID legs counted at 1.00: net = stake * product(won odds) - stake.
    """
    results = [r for _, r in legs_with_results]
    if "LOST" in results:
        return "LOST", -_money(stake)
    if "PENDING" in results:
        return "PENDING", None
    if all(r == "VOID" for r in results):
        return "VOID", 0.0
    adjusted_odds = 1.0
    for odds, result in legs_with_results:
        adjusted_odds *= odds if result == "WON" else 1.0
    return "WON", _money(stake * adjusted_odds - stake)


def recalculate_user_bets(tip_ids=None):
    """Re-grade user slips from their legs' current results. Called whenever a tip result changes.

    With `tip_ids`, only slips containing one of those tips are re-graded; otherwise all of them
    (covers admin overrides that turn a settled leg back to PENDING or flip WON/LOST).
    Returns the number of slips whose status or profit changed.
    """
    wanted = {int(t) for t in tip_ids} if tip_ids else None
    changed = 0
    with get_connection() as conn:
        bets = [dict(r) for r in conn.execute("SELECT * FROM user_bets")]
        bets = [b for b in bets if wanted is None or wanted & set(json.loads(b["tip_ids"] or "[]"))]
        if not bets:
            return 0
        needed = {leg["tip_id"] for b in bets for leg in json.loads(b["legs"] or "[]")}
        marks = ",".join("?" * len(needed)) or "NULL"
        tips = {r["id"]: dict(r) for r in conn.execute(
            f"SELECT id, result, alt_result FROM tips WHERE id IN ({marks})", list(needed))}
        for b in bets:
            legs = json.loads(b["legs"] or "[]")
            if not legs:  # defensive: price-less slip built only from tip_ids
                legs = [{"tip_id": t, "odds": 1.0} for t in json.loads(b["tip_ids"] or "[]")]
            status, net = grade_slip(b["stake_amount"], [(l["odds"], _leg_result(l, tips)) for l in legs])
            if (status, net) != (b["status"], b["net_profit"]):
                conn.execute("UPDATE user_bets SET status = ?, net_profit = ? WHERE id = ?", (status, net, b["id"]))
                changed += 1
    return changed


def get_user_bankroll_stats(user_id, period_days):
    """Bankroll summary for slips logged in the last `period_days` days (7 / 30 / 365).

    Win rate = WON / (WON + LOST). Total Staked counts every slip in the period; Total Payout is what
    settled slips returned (WON: stake + profit, VOID: stake back, LOST: 0); ROI = net / settled stake.
    """
    bets = list_user_bets(user_id, days=period_days)
    settled = [b for b in bets if b["status"] != "PENDING"]
    won = sum(b["status"] == "WON" for b in bets)
    lost = sum(b["status"] == "LOST" for b in bets)
    settled_stake = sum(b["stake_amount"] for b in settled)
    net = sum(b["net_profit"] or 0 for b in settled)
    return {
        "period_days": period_days,
        "total_slips": len(bets),
        "won": won,
        "lost": lost,
        "void": sum(b["status"] == "VOID" for b in bets),
        "pending": len(bets) - len(settled),
        "win_rate": round(100.0 * won / (won + lost), 1) if won + lost else None,
        "total_staked": _money(sum(b["stake_amount"] for b in bets)),
        "settled_staked": _money(settled_stake),
        "pending_staked": _money(sum(b["stake_amount"] for b in bets if b["status"] == "PENDING")),
        "total_payout": _money(sum(b["stake_amount"] + (b["net_profit"] or 0) for b in settled)),
        "net_profit": _money(net),
        "roi": round(100.0 * net / settled_stake, 1) if settled_stake else None,
    }


if __name__ == "__main__":
    import sys
    init_db()
    if "--reset-admin-password" in sys.argv:
        creds = reset_admin_password()
        print(f"Admin password reset. Log in with {creds['email'] or creds['phone_number']} / {creds['password']}")
    print(f"Database ready at {DB_PATH}")

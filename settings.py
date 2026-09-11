"""Runtime-editable connection profiles, so the app can target any Exastro
environment without editing `.env` and restarting.

Model
-----
* A **profile** is one named target: gateway URL, org, workspace, credentials,
  execution environment, timeouts — and, under *Advanced*, the menu names and
  numeric ids that differ between ITA versions.
* Exactly one profile is **active**. Selecting it rewrites the `config` module
  constants and rebuilds the client, so the 60+ `cfg.` call sites in
  `exastro_client.py` keep working untouched.
* `.env` is only a **seed**: on first run an "Initial (.env)" profile is created
  from it. Afterwards the database wins, and `.env` is never rewritten — so a
  broken setting can always be undone by deleting `settings.db`.

Secrets
-------
The refresh token and password live in this database as plain text. That is a
deliberate trade-off for a single-operator tool with no key management available
(cryptography is not installed in this venv), and it is bounded by three rules:
they are never serialized to the browser, never logged, and the file is created
mode 0600. Anyone who can read `settings.db` can spend the token — treat the
host accordingly.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
import time
from datetime import datetime

import config as cfg

SETTINGS_DB = os.getenv("EXA_SETTINGS_DB", "settings.db")

# Fields that hold credentials. Nothing in this list may ever leave the server
# in a rendered page or a JSON response — only `secret_hint()` about them.
SECRET_FIELDS = ("API_TOKEN", "PASSWORD")

# How long a verified PIN keeps the settings page open, and how wrong guesses
# are punished.
UNLOCK_TTL = 15 * 60
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 60

# ---------------------------------------------------------------------------
# The field table: storage, validation and the form all read this.
# group: connection | auth | tuning | advanced
# ---------------------------------------------------------------------------

FIELDS: list[dict] = [
    # ---- connection -------------------------------------------------------
    {"key": "GATEWAY_URL", "kind": "text", "group": "connection",
     "label": "set_gateway", "required": True,
     "hint": "set_gateway_hint", "placeholder": "http://192.0.2.10:4040"},
    {"key": "ORG_ID", "kind": "text", "group": "connection",
     "label": "set_org", "required": True, "placeholder": "acme"},
    {"key": "WORKSPACE_ID", "kind": "text", "group": "connection",
     "label": "set_workspace", "required": True, "placeholder": "demo_ws"},
    {"key": "KEYCLOAK_URL", "kind": "text", "group": "connection",
     "label": "set_keycloak", "optional": True,
     "hint": "set_keycloak_hint"},
    # ---- credentials ------------------------------------------------------
    {"key": "API_TOKEN", "kind": "secret", "group": "auth",
     "label": "set_token", "hint": "set_token_hint"},
    {"key": "USER", "kind": "text", "group": "auth",
     "label": "set_user", "optional": True, "hint": "set_user_hint"},
    {"key": "PASSWORD", "kind": "secret", "group": "auth",
     "label": "set_password", "optional": True},
    {"key": "CLIENT_ID", "kind": "text", "group": "auth",
     "label": "set_client_id", "optional": True, "hint": "set_client_id_hint"},
    # ---- tuning -----------------------------------------------------------
    {"key": "EXEC_ENV", "kind": "text", "group": "tuning",
     "label": "set_exec_env", "optional": True, "hint": "set_exec_env_hint"},
    {"key": "VAR_TIMEOUT", "kind": "int", "group": "tuning",
     "label": "set_var_timeout", "min": 0, "max": 3600},
    {"key": "TIMEOUT", "kind": "int", "group": "tuning",
     "label": "set_timeout", "min": 1, "max": 600},
    {"key": "AUTH_TIMEOUT", "kind": "int", "group": "tuning",
     "label": "set_auth_timeout", "min": 1, "max": 600},
    {"key": "VERIFY_TLS", "kind": "bool", "group": "tuning",
     "label": "set_verify_tls", "hint": "set_verify_tls_hint"},
    # ---- advanced: names and ids that move between ITA versions ----------
    {"key": "MOVEMENT_MENU", "kind": "text", "group": "advanced",
     "label": "adv_movement_menu"},
    {"key": "ROLE_MENU", "kind": "text", "group": "advanced",
     "label": "adv_role_menu"},
    {"key": "ROLE_LINK_MENU", "kind": "text", "group": "advanced",
     "label": "adv_role_link_menu"},
    {"key": "FILE_LINK_MENU", "kind": "text", "group": "advanced",
     "label": "adv_file_link_menu"},
    {"key": "SUBST_MENU", "kind": "text", "group": "advanced",
     "label": "adv_subst_menu"},
    {"key": "EXECUTE_MENU", "kind": "text", "group": "advanced",
     "label": "adv_execute_menu"},
    {"key": "HEADER_SECTION", "kind": "text", "group": "advanced",
     "label": "adv_header_section"},
    {"key": "ORCHESTRATOR_ID", "kind": "text", "group": "advanced",
     "label": "adv_orchestrator_id", "hint": "id_wins_hint", "options_from": "orchestrator", "pair": "ORCHESTRATOR"},
    {"key": "HOST_SPECIFIC_FORMAT_ID", "kind": "text", "group": "advanced",
     "label": "adv_host_format_id", "hint": "id_wins_hint", "options_from": "host_specific_format", "pair": "HOST_SPECIFIC_FORMAT"},
    {"key": "SHEET_TYPE_ID", "kind": "text", "group": "advanced",
     "label": "adv_sheet_type_id", "hint": "id_wins_hint", "options_from": "sheet_type"},
    {"key": "SUBST_REGISTRATION_METHOD_ID", "kind": "text", "group": "advanced",
     "label": "adv_subst_method_id", "hint": "id_wins_hint", "options_from": "registration_method", "pair": "SUBST_REGISTRATION_METHOD"},
    {"key": "FILE_LINK_ROLE_TYPE_ID", "kind": "text", "group": "advanced",
     "label": "adv_link_file_type_id", "hint": "id_wins_hint", "options_from": "link_file_type", "pair": "FILE_LINK_ROLE_TYPE"},
    {"key": "MENU_GROUP_INPUT_ID", "kind": "text", "group": "advanced",
     "label": "adv_group_input_id", "options_from": "menu_group"},
    {"key": "MENU_GROUP_SUBST_ID", "kind": "text", "group": "advanced",
     "label": "adv_group_subst_id", "options_from": "menu_group"},
    {"key": "MENU_GROUP_REF_ID", "kind": "text", "group": "advanced",
     "label": "adv_group_ref_id", "options_from": "menu_group"},
    # ---- paired display strings: written from the chosen id, never edited ----
    {"key": "ORCHESTRATOR", "kind": "text", "group": "advanced",
     "label": "adv_orchestrator", "hidden": True},
    {"key": "HOST_SPECIFIC_FORMAT", "kind": "text", "group": "advanced",
     "label": "adv_host_format", "hidden": True},
    {"key": "SUBST_REGISTRATION_METHOD", "kind": "text", "group": "advanced",
     "label": "adv_subst_method", "hidden": True},
    {"key": "FILE_LINK_ROLE_TYPE", "kind": "text", "group": "advanced",
     "label": "adv_link_file_type", "hidden": True},
    {"key": "ADMIN_ROLE", "kind": "text", "group": "advanced",
     "label": "set_admin_role", "hint": "set_admin_role_hint"},
]

BY_KEY = {f["key"]: f for f in FIELDS}
INT_FIELDS = [f["key"] for f in FIELDS if f["kind"] == "int"]
BOOL_FIELDS = [f["key"] for f in FIELDS if f["kind"] == "bool"]


def env_defaults() -> dict:
    """The current `config` values — i.e. whatever `.env` established."""
    out = {}
    for f in FIELDS:
        value = getattr(cfg, f["key"], "")
        out[f["key"]] = bool(value) if f["kind"] == "bool" else str(value or "")
    # CLIENT_ID is *derived* from the org, so reporting config's value here
    # would pin `_dat-api` into every profile and stop the org field from ever
    # changing it. Blank means "derive it".
    out["CLIENT_ID"] = ""
    return out


def derived(values: dict) -> dict:
    """Fields computed from others, so the form never asks for them."""
    gateway = (values.get("GATEWAY_URL") or "").rstrip("/")
    org = values.get("ORG_ID") or ""
    ws = values.get("WORKSPACE_ID") or ""
    client_id = ((values.get("CLIENT_ID") or "").strip()
                 or (f"_{org}-api" if org else ""))
    return {
        "API_BASE": f"{gateway}/api/{org}/workspaces/{ws}/ita",
        "TOKEN_URL": f"{gateway}/auth/realms/{org}/protocol/openid-connect/token",
        "CLIENT_ID": client_id,
    }


# What a write needs before it can even attempt to reach Exastro. Everything
# here is environment-specific, i.e. exactly what a shipped copy cannot know.
REQUIRED_FIELDS = ("GATEWAY_URL", "ORG_ID", "WORKSPACE_ID")


def effective(values: dict | None = None) -> dict:
    """Config values as they stand now, with the profile merged in."""
    merged = env_defaults()
    merged.update((values if values is not None else
                   (active_profile() or {}).get("payload")) or {})
    merged.update({k: v for k, v in derived(merged).items() if v})
    return merged


def has_target(values: dict | None = None) -> bool:
    v = effective(values)
    return all(str(v.get(k) or "").strip() for k in REQUIRED_FIELDS)


def has_credentials(values: dict | None = None) -> bool:
    v = effective(values)
    return bool(str(v.get("API_TOKEN") or "").strip()
                or (str(v.get("USER") or "").strip()
                    and str(v.get("PASSWORD") or "").strip()))


def is_configured(values: dict | None = None) -> bool:
    """Can this install talk to Exastro at all? False means: ask, don't fail."""
    return has_target(values) and has_credentials(values)


def missing_labels() -> list[str]:
    """Human names for what is still unset, for the first-run banner."""
    v = effective()
    out = [BY_KEY[k]["label"] for k in REQUIRED_FIELDS
           if not str(v.get(k) or "").strip()]
    if not has_credentials(v):
        out.append("set_token")
    return out


def apply(values: dict) -> dict:
    """Push a profile's values onto the `config` module.

    Returns the effective merged set, because callers need to know what a blank
    field actually resolved to. Unknown keys are ignored rather than raised:
    a profile saved by a newer build must not break an older one.
    """
    merged = env_defaults()          # fall back to .env for anything unset
    merged.update({k: v for k, v in (values or {}).items() if k in BY_KEY})
    for key in INT_FIELDS:
        try:
            merged[key] = int(str(merged[key]).strip() or 0)
        except (TypeError, ValueError):
            merged[key] = int(getattr(cfg, key, 0) or 0)
    for key in BOOL_FIELDS:
        merged[key] = bool(merged[key]) if isinstance(merged[key], bool) \
            else str(merged[key]).strip().lower() in ("1", "true", "on", "yes")

    for key, value in merged.items():
        setattr(cfg, key, value)
    for key, value in derived(merged).items():
        setattr(cfg, key, value)
    return merged


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    fresh = not os.path.exists(SETTINGS_DB)
    conn = sqlite3.connect(SETTINGS_DB)
    conn.row_factory = sqlite3.Row
    if fresh:
        try:
            os.chmod(SETTINGS_DB, stat.S_IRUSR | stat.S_IWUSR)   # 0600
        except OSError:
            pass                                                 # best effort
    return conn


def init_store() -> None:
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS profiles(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                payload TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meta(
                key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
        count = conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
        if count == 0:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            seed = {"name": "Initial (.env)", "payload": env_defaults()}
            # Only seed credentials that .env actually supplied; an empty
            # profile is honest about being unfinished.
            conn.execute(
                "INSERT INTO profiles (name, payload, active, created_at,"
                " updated_at) VALUES (?,?,1,?,?)",
                (seed["name"], json.dumps(seed["payload"], ensure_ascii=False),
                 now, now))


def _row_profile(row) -> dict:
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        payload = {}
    return {"id": row["id"], "name": row["name"], "payload": payload,
            "active": bool(row["active"]), "created_at": row["created_at"],
            "updated_at": row["updated_at"]}


def list_profiles() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM profiles ORDER BY active DESC, name").fetchall()
    return [_row_profile(r) for r in rows]


def get_profile(profile_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM profiles WHERE id = ?",
                           (profile_id,)).fetchone()
    return _row_profile(row) if row else None


def active_profile() -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM profiles WHERE active = 1 LIMIT 1"
                           ).fetchone()
    return _row_profile(row) if row else None


def save_profile(name: str, payload: dict, profile_id: int | None = None,
                 clear=()) -> int:
    """Insert or update, keeping stored secrets when the form left them blank."""
    name = (name or "").strip()
    if not name:
        raise ValueError("profile name is required")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    clean = {k: v for k, v in payload.items() if k in BY_KEY}
    with _connect() as conn:
        if profile_id:
            row = conn.execute("SELECT payload FROM profiles WHERE id = ?",
                               (profile_id,)).fetchone()
            if row is None:
                raise KeyError(f"no profile {profile_id}")
            stored = json.loads(row["payload"] or "{}")
            clear = tuple(clear)
            for key in SECRET_FIELDS:
                # A blank secret means "keep what is stored"; the form never
                # receives the real value, so it cannot resubmit it.
                if key in clear:
                    clean[key] = ""            # the form asked to drop it
                elif not str(clean.get(key) or "").strip():
                    clean[key] = stored.get(key, "")
            conn.execute("UPDATE profiles SET name=?, payload=?, updated_at=?"
                         " WHERE id=?",
                         (name, json.dumps(clean, ensure_ascii=False), now,
                          profile_id))
            return profile_id
        conn.execute("INSERT INTO profiles (name, payload, active, created_at,"
                     " updated_at) VALUES (?,?,0,?,?)",
                     (name, json.dumps(clean, ensure_ascii=False), now, now))
        return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def delete_profile(profile_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
        if not cur.rowcount:
            return False
        still_active = conn.execute(
            "SELECT id FROM profiles WHERE active=1").fetchone()
        if still_active is None:                     # never leave zero active
            first = conn.execute("SELECT id FROM profiles ORDER BY id"
                                 " LIMIT 1").fetchone()
            if first:
                conn.execute("UPDATE profiles SET active=1 WHERE id=?",
                             (first["id"],))
    return True


def activate_profile(profile_id: int) -> dict | None:
    """Make one profile current. Returns it, or None if the id does not exist."""
    with _connect() as conn:
        if conn.execute("SELECT id FROM profiles WHERE id=?",
                        (profile_id,)).fetchone() is None:
            return None
        conn.execute("UPDATE profiles SET active=0")
        conn.execute("UPDATE profiles SET active=1 WHERE id=?", (profile_id,))
        row = conn.execute("SELECT * FROM profiles WHERE id=?",
                           (profile_id,)).fetchone()
    profile = _row_profile(row)
    apply(profile["payload"])
    return profile


def apply_active() -> dict:
    """Load the active profile onto `config` at start-up.

    With no profile at all (fresh install, deleted database) the `.env` values
    already in `config` stand, so the app still boots and says so.
    """
    profile = active_profile()
    if profile is None:
        cfg.UI_LANG = cfg.UI_LANG               # nothing to change
        return dict(env_defaults())
    return apply(profile["payload"])


# ---------------------------------------------------------------------------
# secrets: describe them, never reveal them
# ---------------------------------------------------------------------------

def secret_hint(value: str) -> str:
    """'set · ••••wxyz' or 'not set' — enough to recognise a token, not use it."""
    text = str(value or "").strip()
    if not text:
        return ""
    return f"set · ••••{text[-4:]}"


def public_payload(profile: dict) -> dict:
    """A profile with its secrets replaced by hints, safe to send to a browser."""
    payload = dict(profile.get("payload") or {})
    for key in SECRET_FIELDS:
        payload[f"{key}_hint"] = secret_hint(payload.get(key))
        payload.pop(key, None)
    return payload


# ---------------------------------------------------------------------------
# settings PIN
# ---------------------------------------------------------------------------

def _meta_get(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _meta_set(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES (?,?)"
                 " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))


def pin_configured() -> bool:
    if os.getenv("EXA_SETTINGS_PIN"):
        return True
    with _connect() as conn:
        return bool(_meta_get(conn, "pin"))


def _hash_pin(pin: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"),
                               bytes.fromhex(salt), 120_000).hex()


def set_pin(pin: str) -> None:
    pin = str(pin or "")
    if len(pin) < 4:
        raise ValueError("PIN must be at least 4 characters")
    salt = secrets.token_hex(16)
    with _connect() as conn:
        _meta_set(conn, "pin", f"{salt}${_hash_pin(pin, salt)}")


def _lock_state(conn) -> float:
    """Seconds still to wait, or 0."""
    raw = _meta_get(conn, "pin_lock")
    if not raw:
        return 0.0
    try:
        until = float(raw)
    except ValueError:
        return 0.0
    return max(0.0, until - time.time())


def check_pin(pin: str) -> tuple[bool, float]:
    """Verify the PIN. Returns (ok, seconds_locked)."""
    env_pin = os.getenv("EXA_SETTINGS_PIN")
    with _connect() as conn:
        wait = _lock_state(conn)
        if wait > 0:
            return False, wait
        stored = _meta_get(conn, "pin")
        if env_pin:
            ok = secrets.compare_digest(str(pin), env_pin)
        elif not stored:
            return False, 0.0                     # bootstrap handled by caller
        else:
            salt, _, digest = str(stored).partition("$")
            try:
                ok = secrets.compare_digest(_hash_pin(str(pin or ""), salt),
                                            digest)
            except ValueError:                    # salt not hex -> unusable
                ok = False
        if ok:
            _meta_set(conn, "pin_fails", "0")
            return True, 0.0
        try:
            fails = int(_meta_get(conn, "pin_fails") or 0) + 1
        except ValueError:
            fails = 1
        _meta_set(conn, "pin_fails", str(fails))
        if fails >= MAX_ATTEMPTS and not env_pin:
            _meta_set(conn, "pin_lock", str(time.time() + LOCKOUT_SECONDS))
            _meta_set(conn, "pin_fails", "0")
            return False, float(LOCKOUT_SECONDS)
        return False, 0.0


def attempts_left() -> int:
    with _connect() as conn:
        try:
            fails = int(_meta_get(conn, "pin_fails") or 0)
        except ValueError:
            fails = 0
    return max(0, MAX_ATTEMPTS - fails)


# The settings store is only as private as the database file; make the intent
# explicit for anyone reading `ls -l`.
__all__ = [name for name in dir() if not name.startswith("_")]

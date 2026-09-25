"""Runtime-editable connection profiles, so the app can target any Exastro
environment without editing `.env` and restarting.

Model
-----
* A **profile** is one named target: gateway URL, org, workspace, credentials,
  execution environment, timeouts — and, under *Advanced*, the menu names and
  numeric ids that differ between ITA versions.
* Each user owns a private set of profiles, with at most one active profile
  for that user. Legacy callers that omit ``owner`` keep the original
  process-wide behaviour, while the app resolves a per-user client for requests.
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

# Three roles, in descending order of what they may do.
#
#   admin  - everything: accounts, who gets which profile, and any profile
#   coadmin- may create and edit profiles of their own, but delete nothing and
#            manage nobody
#   user   - may only *use* the profiles an admin assigned to them; they cannot
#            create, edit or delete a profile, and never see a stored secret
ROLE_ADMIN = "admin"
ROLE_COADMIN = "coadmin"
ROLE_USER = "user"
ROLES = (ROLE_ADMIN, ROLE_COADMIN, ROLE_USER)
# Only these two may create a profile. A `user` is a consumer of somebody
# else's configuration, which is the whole point of assigning them one.
PROFILE_ROLES = (ROLE_ADMIN, ROLE_COADMIN)


def normalise_role(role: str | None) -> str:
    """Anything unrecognised is the least privileged thing we can offer."""
    role = str(role or "").strip().lower()
    return role if role in ROLES else ROLE_USER


# The two groups that identify *this* connection and *this* person's credentials.
# A new account must be asked for these; the rest of the table is shared
# configuration (ITA menu names, timeouts, ids) that a fresh install already
# knows and that nobody should have to retype per person.
PRIVATE_GROUPS = ("connection", "auth")

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
    {"key": "OPERATION_MENU", "kind": "text", "group": "advanced",
     "label": "adv_operation_menu"},
    {"key": "HOSTGROUP_MENU", "kind": "text", "group": "advanced",
     "label": "adv_hostgroup_menu"},
    {"key": "HOST_LINK_MENU", "kind": "text", "group": "advanced",
     "label": "adv_host_link_menu"},
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
    merged.update({k: v for k, v in (values if values is not None else
                   (active_profile() or {}).get("payload") or {}).items()
                   if k in BY_KEY})
    # Coerced, not just merged: a per-user client is built straight from this
    # dict, so a TIMEOUT left as "90" reaches the HTTP layer and 500s there.
    coerce_types(merged)
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


def missing_labels(values: dict | None = None) -> list[str]:
    """Human names for what is still unset, for the first-run banner.

    Passing one profile's values keeps a per-user setup check from reading the
    process-wide active profile. The no-argument form retains its old meaning.
    """
    v = effective(values)
    out = [BY_KEY[k]["label"] for k in REQUIRED_FIELDS
           if not str(v.get(k) or "").strip()]
    if not has_credentials(v):
        out.append("set_token")
    return out


def coerce_types(merged: dict) -> dict:
    """Turn the stored text back into the types the client expects.

    A profile is saved from a web form, so every number and checkbox arrives as
    a string. The client hands TIMEOUT straight to the HTTP layer, which rejects
    a string with `Timeout value connect was 90` -- so every consumer of a
    profile's values has to pass through here, not just the ones that happen to
    write them onto `config`.
    """
    for key in INT_FIELDS:
        try:
            merged[key] = int(str(merged[key]).strip() or 0)
        except (TypeError, ValueError):
            merged[key] = int(getattr(cfg, key, 0) or 0)
    for key in BOOL_FIELDS:
        merged[key] = bool(merged[key]) if isinstance(merged[key], bool) \
            else str(merged[key]).strip().lower() in ("1", "true", "on", "yes")
    return merged


def apply(values: dict) -> dict:
    """Push a profile's values onto the `config` module.

    Returns the effective merged set, because callers need to know what a blank
    field actually resolved to. Unknown keys are ignored rather than raised:
    a profile saved by a newer build must not break an older one.
    """
    merged = env_defaults()          # fall back to .env for anything unset
    merged.update({k: v for k, v in (values or {}).items() if k in BY_KEY})
    coerce_types(merged)

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
                updated_at TEXT NOT NULL,
                owner TEXT
            )""")
        # SQLite has no ADD COLUMN IF NOT EXISTS. Probe before every boot so an
        # existing installation keeps all of its rows while gaining ownership.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(profiles)")}
        if "owner" not in columns:
            conn.execute("ALTER TABLE profiles ADD COLUMN owner TEXT")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0
            )""")
        # An install created before user management has a users table without
        # the column; add it in place rather than asking anyone to rebuild.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        if "is_admin" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER"
                         " NOT NULL DEFAULT 0")
            if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
                # whoever registered first is the one who has to be trusted to
                # hand out the next accounts
                conn.execute("UPDATE users SET is_admin=1 WHERE id=("
                             "SELECT MIN(id) FROM users)")
        # `is_admin` was a yes/no; three roles need a name.
        if "role" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL"
                         " DEFAULT 'user'")
            conn.execute("UPDATE users SET role = ? WHERE is_admin = 1",
                         (ROLE_ADMIN,))
        # A profile an admin has handed to somebody. Separate from `owner`: the
        # profile stays where it is, so its credentials are stored once and an
        # edit by the owner reaches the user who was given it.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS profile_grants(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                profile_id INTEGER NOT NULL,
                granted_at TEXT NOT NULL,
                UNIQUE(username, profile_id)
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meta(
                key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
        count = conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
        if count == 0:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            seed = {"name": "Initial (.env)", "payload": env_defaults()}
            # Only seed credentials that .env actually supplied; an empty
            # profile is honest about being unfinished. It remains an orphan
            # until the first app user registers and explicitly adopts it.
            conn.execute(
                "INSERT INTO profiles (name, payload, active, created_at,"
                " updated_at, owner) VALUES (?,?,1,?,?,NULL)",
                (seed["name"], json.dumps(seed["payload"], ensure_ascii=False),
                 now, now))


# ---------------------------------------------------------------------------
# app users and profile ownership
# ---------------------------------------------------------------------------

def _normalise_username(username) -> str:
    return str(username or "").strip().lower()


def _hash_password(password: str, salt: str) -> str:
    """PBKDF2-SHA256 with the same storage shape as the Settings PIN."""
    return hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"),
                               bytes.fromhex(salt), 120_000).hex()


def _row_user(row) -> dict | None:
    if row is None:
        return None
    return {"id": row["id"], "username": row["username"],
            "password_hash": row["password_hash"],
            "created_at": row["created_at"]}


def create_user(username: str, password: str, role: str | None = None,
                profile_ids=()) -> bool:
    """Create an app user, with a role and any profiles granted to them.

    The first account on an install is always an admin: somebody has to be able
    to hand out the rest, and on a fresh install that somebody is whoever
    arrived first. Grants are applied in the same transaction, so an account is
    never briefly alive without the profile it was created for.
    """
    username = _normalise_username(username)
    if (len(username) < 3 or any(ch.isspace() for ch in username)
            or not str(password or "")):
        return False
    role = normalise_role(role)
    salt = secrets.token_hex(16)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _connect() as conn:
            first = not conn.execute(
                "SELECT COUNT(*) FROM users").fetchone()[0]
            if first:
                role = ROLE_ADMIN
            conn.execute(
                "INSERT INTO users (username, password_hash, created_at,"
                " is_admin, role) VALUES (?,?,?,?,?)",
                (username, f"{salt}${_hash_password(password, salt)}", now,
                 1 if role == ROLE_ADMIN else 0, role))
            for pid in _clean_ids(profile_ids):
                # A grant is only useful if the profile really exists.
                if conn.execute("SELECT id FROM profiles WHERE id=?",
                                (pid,)).fetchone():
                    conn.execute(
                        "INSERT OR IGNORE INTO profile_grants"
                        " (username, profile_id, granted_at) VALUES (?,?,?)",
                        (username, pid, now))
    except sqlite3.IntegrityError:
        return False
    return True


def _clean_ids(values) -> list[int]:
    out = []
    for value in values or ():
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue
    return out


def set_role(username: str, role: str) -> bool:
    username = _normalise_username(username)
    role = normalise_role(role)
    with _connect() as conn:
        if conn.execute("SELECT id FROM users WHERE username=?",
                        (username,)).fetchone() is None:
            return False
        if role != ROLE_ADMIN and not _another_admin_exists(conn, username):
            return False       # never leave the install with no admin
        conn.execute("UPDATE users SET role=?, is_admin=? WHERE username=?",
                     (role, 1 if role == ROLE_ADMIN else 0, username))
    return True


def _another_admin_exists(conn, exclude: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM users WHERE role=? AND username<>? LIMIT 1",
        (ROLE_ADMIN, _normalise_username(exclude))).fetchone())


def role_of(username: str) -> str:
    """This account's role; an unknown account is a plain user, never an admin."""
    with _connect() as conn:
        row = conn.execute("SELECT role FROM users WHERE username=?",
                           (_normalise_username(username),)).fetchone()
    if row is None:
        return ROLE_USER
    return normalise_role(row["role"])


def can_manage_profiles(username: str) -> bool:
    """May this person create, edit or delete a profile of their own?"""
    return role_of(username) in PROFILE_ROLES


def can_delete_profiles(username: str) -> bool:
    """Deleting is destructive and unrecoverable, so it is admin-only."""
    return role_of(username) == ROLE_ADMIN


def can_manage_users(username: str) -> bool:
    return role_of(username) == ROLE_ADMIN


def is_admin(username: str) -> bool:
    """Kept for callers that only ever asked "is this the top level?"."""
    return can_manage_users(username)


def list_users() -> list[dict]:
    order = ", ".join(f"'{r}'" for r in ROLES)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT username, created_at, role FROM users"
            f" ORDER BY CASE role WHEN 'admin' THEN 0 WHEN 'coadmin' THEN 1"
            f" ELSE 2 END, username").fetchall()
        out = []
        for r in rows:
            grants = conn.execute(
                "SELECT profile_id FROM profile_grants WHERE username=?"
                " ORDER BY profile_id", (r["username"],)).fetchall()
            out.append({"username": r["username"],
                        "created_at": r["created_at"],
                        "role": normalise_role(r["role"]),
                        "is_admin": normalise_role(r["role"]) == ROLE_ADMIN,
                        "granted": [g["profile_id"] for g in grants]})
    return out


def delete_user(username: str) -> bool:
    """Remove an account and every profile it owned.

    Refuses the last admin, because an install nobody can add accounts to is
    an install nobody can recover.
    """
    username = _normalise_username(username)
    with _connect() as conn:
        row = conn.execute("SELECT is_admin FROM users WHERE username=?",
                           (username,)).fetchone()
        if row is None:
            return False
        admins = conn.execute("SELECT COUNT(*) FROM users WHERE is_admin=1"
                              ).fetchone()[0]
        if row["is_admin"] and admins <= 1:
            return False
        conn.execute("DELETE FROM profiles WHERE owner=?", (username,))
        conn.execute("DELETE FROM users WHERE username=?", (username,))
    return True


def authenticate(username: str, password: str) -> bool:
    """Verify one app user without revealing whether just the name or the pair failed."""
    username = _normalise_username(username)
    with _connect() as conn:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE username=?",
            (username,)).fetchone()
    stored = str(row["password_hash"] or "") if row else ""
    salt, _, digest = stored.partition("$")
    try:
        actual = _hash_password(password, salt)
    except ValueError:
        # Still perform the expensive primitive for an absent/corrupt row, then
        # return the same answer without turning a bad salt into a 500.
        _hash_password(password, "00" * 16)
        return False
    return secrets.compare_digest(actual, digest)


def user_count() -> int:
    with _connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def get_user(username: str) -> dict | None:
    username = _normalise_username(username)
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?",
                           (username,)).fetchone()
    return _row_user(row)


def _row_profile(row) -> dict:
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        payload = {}
    owner = row["owner"] if "owner" in row.keys() else None
    return {"id": row["id"], "name": row["name"], "payload": payload,
            "active": bool(row["active"]), "created_at": row["created_at"],
            "updated_at": row["updated_at"], "owner": owner or None}


def list_profiles(owner: str | None = None) -> list[dict]:
    """Profiles one person may *see* in Settings.

    Their own, plus any profile an admin granted them. A grant is read and
    use, not ownership, so `granted` says which is which and the caller decides
    what to do with that -- the settings page strips the secrets.
    """
    with _connect() as conn:
        if owner is None:
            rows = conn.execute(
                "SELECT * FROM profiles ORDER BY active DESC, name"
            ).fetchall()
        else:
            name = _normalise_username(owner)
            rows = conn.execute(
                "SELECT * FROM profiles WHERE owner=? ORDER BY active DESC, name",
                (name,)).fetchall()
            granted = conn.execute(
                "SELECT p.* FROM profiles p JOIN profile_grants g"
                " ON g.profile_id = p.id WHERE g.username=? AND p.owner<>?"
                " ORDER BY p.name", (name, name)).fetchall()
            rows = list(rows) + list(granted)
    out = []
    for r in rows:
        profile = _row_profile(r)
        profile["granted"] = (owner is not None
                              and profile.get("owner") != _normalise_username(owner))
        out.append(profile)
    return out


def get_profile(profile_id: int, owner: str | None = None) -> dict | None:
    with _connect() as conn:
        if owner is None:
            row = conn.execute("SELECT * FROM profiles WHERE id = ?",
                               (profile_id,)).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM profiles WHERE id = ? AND owner = ?",
                (profile_id, _normalise_username(owner))).fetchone()
    return _row_profile(row) if row else None


def active_profile(owner: str | None = None) -> dict | None:
    with _connect() as conn:
        if owner is None:
            row = conn.execute(
                "SELECT * FROM profiles WHERE active = 1 ORDER BY id LIMIT 1"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM profiles WHERE active = 1 AND owner = ?"
                " ORDER BY id LIMIT 1",
                (_normalise_username(owner),)).fetchone()
    return _row_profile(row) if row else None


def own_active_profile(username: str) -> dict | None:
    """The profile this person's next run will use.

    Preference order: the one they activated, then one they own, then one an
    admin granted them. The last case is how a plain `user` -- who by design
    owns nothing -- reaches an Exastro at all.

    Ownership is recorded on the result so callers can tell "this is mine and I
    may edit it" from "this was lent to me and I may only run against it".
    """
    username = _normalise_username(username)
    active = active_profile(username)
    if active is not None:
        active = dict(active)
        active["granted"] = active.get("owner") != username
        return active
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM profiles WHERE owner=? ORDER BY id LIMIT 1",
            (username,)).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT p.* FROM profiles p JOIN profile_grants g"
                " ON g.profile_id = p.id WHERE g.username=? ORDER BY p.id"
                " LIMIT 1", (username,)).fetchone()
    if row is None:
        return None
    profile = dict(_row_profile(row))
    profile["granted"] = profile.get("owner") != username
    return profile


def own_effective(username: str) -> dict | None:
    """This user's resolved values, or None when they have no profile at all.

    Two different kinds of value are merged here, and conflating them caused a
    real outage:

    * the **private groups** -- connection and credentials -- come from this
      person's profile and nowhere else. The `.env` holds whoever installed the
      tool; letting its API token backfill over a colleague's username and
      password meant `token()` (which prefers a token) used the installer's
      credentials and Exastro answered 403, with the form meanwhile claiming
      `set · ••••xxxx` for a profile that had no token at all.
    * the **shared groups** -- menu names, timeouts, ids -- do fall back to
      `.env`, because that is shared setup rather than a secret.

    Returns None when the person has no profile: that is an answer, not an
    invitation to fall back.
    """
    profile = own_active_profile(username)
    if profile is None:
        return None
    payload = {k: v for k, v in (profile.get("payload") or {}).items()
               if k in BY_KEY}
    merged = env_defaults()
    for field in FIELDS:
        key = field["key"]
        if field["group"] in PRIVATE_GROUPS and key not in payload:
            # This person set nothing here. Blank, never the installer's.
            merged[key] = False if field["kind"] == "bool" else ""
        else:
            merged[key] = payload.get(key, merged.get(key, ""))
    coerce_types(merged)
    merged.update({k: v for k, v in derived(merged).items() if v})
    return merged


def granted_profile_ids(username: str) -> list[int]:
    username = _normalise_username(username)
    with _connect() as conn:
        return [r["profile_id"] for r in conn.execute(
            "SELECT profile_id FROM profile_grants WHERE username=?"
            " ORDER BY profile_id", (username,)).fetchall()]


def set_grants(username: str, profile_ids=()) -> bool:
    """Replace this account's assigned profiles.

    Only an admin may call this, and a grant never makes the recipient the
    owner: the profile stays where it is, so a later edit by its owner is
    picked up and the stored token exists in exactly one place.
    """
    username = _normalise_username(username)
    with _connect() as conn:
        if conn.execute("SELECT id FROM users WHERE username=?",
                        (username,)).fetchone() is None:
            return False
        wanted = [pid for pid in _clean_ids(profile_ids)
                  if conn.execute("SELECT id FROM profiles WHERE id=?",
                                  (pid,)).fetchone()]
        conn.execute("DELETE FROM profile_grants WHERE username=?", (username,))
        for pid in wanted:
            conn.execute("INSERT OR IGNORE INTO profile_grants"
                         " (username, profile_id, granted_at) VALUES (?,?,?)",
                         (username, pid, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    return True


def may_edit_profile(username: str, profile_id: int) -> bool:
    """May this person open this profile *for editing*?

    Own profile and a profile-managing role, or nothing. A granted profile is
    readable and runnable, but editing it would mean editing somebody else's
    stored credentials.
    """
    if not can_manage_profiles(username):
        return False
    return get_profile(profile_id, owner=username) is not None


def usable_profile_ids(username: str) -> list[int]:
    """Every profile this person may point a run at: their own plus grants.

    A `user` has no profiles of their own by design, so this is the only way
    they can reach an Exastro at all.
    """
    with _connect() as conn:
        own = [r["id"] for r in conn.execute(
            "SELECT id FROM profiles WHERE owner=?"
            " ORDER BY id", (_normalise_username(username),)).fetchall()]
        granted = [r["profile_id"] for r in conn.execute(
            "SELECT profile_id FROM profile_grants WHERE username=?"
            " ORDER BY profile_id", (_normalise_username(username),)).fetchall()]
    seen, out = set(), []
    for pid in (*own, *granted):
        if pid not in seen:
            seen.add(pid)
            out.append(pid)
    return out


def may_use_profile(username: str, profile_id: int) -> bool:
    """Is this profile one this person is allowed to *run* against?

    Read and use, but not edit: being handed a profile is not being given the
    credentials or the ability to redirect somebody else's Exastro.
    """
    try:
        return int(profile_id) in usable_profile_ids(username)
    except (TypeError, ValueError):
        return False


def adopt_orphans(username: str) -> int:
    """Give every pre-user profile to the first registered app user."""
    username = _normalise_username(username)
    if get_user(username) is None:
        raise KeyError(f"no user {username}")
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE profiles SET owner=? WHERE owner IS NULL OR TRIM(owner)=''",
            (username,))
        return int(cur.rowcount)


def save_profile(name: str, payload: dict, profile_id: int | None = None,
                 clear=(), owner: str | None = None) -> int:
    """Insert or update, keeping stored secrets when the form left them blank.

    With an owner, every update is a two-column lookup: a guessed id owned by
    somebody else is indistinguishable here from an id that does not exist.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("profile name is required")
    owner_name = _normalise_username(owner) if owner is not None else None
    if owner is not None and not owner_name:
        raise ValueError("profile owner is required")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    clean = {k: v for k, v in payload.items() if k in BY_KEY}
    with _connect() as conn:
        if profile_id:
            if owner_name is None:
                row = conn.execute("SELECT payload FROM profiles WHERE id = ?",
                                   (profile_id,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT payload FROM profiles WHERE id = ? AND owner = ?",
                    (profile_id, owner_name)).fetchone()
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
            if owner_name is None:
                conn.execute("UPDATE profiles SET name=?, payload=?, updated_at=?"
                             " WHERE id=?",
                             (name, json.dumps(clean, ensure_ascii=False), now,
                              profile_id))
            else:
                conn.execute("UPDATE profiles SET name=?, payload=?, updated_at=?"
                             " WHERE id=? AND owner=?",
                             (name, json.dumps(clean, ensure_ascii=False), now,
                              profile_id, owner_name))
            return profile_id
        conn.execute("INSERT INTO profiles (name, payload, active, created_at,"
                     " updated_at, owner) VALUES (?,?,0,?,?,?)",
                     (name, json.dumps(clean, ensure_ascii=False), now, now,
                      owner_name))
        return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def delete_profile(profile_id: int, owner: str | None = None) -> bool:
    owner_name = _normalise_username(owner) if owner is not None else None
    with _connect() as conn:
        if owner_name is None:
            cur = conn.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
        else:
            cur = conn.execute(
                "DELETE FROM profiles WHERE id=? AND owner=?",
                (profile_id, owner_name))
        if not cur.rowcount:
            return False
        if owner_name is None:
            still_active = conn.execute(
                "SELECT id FROM profiles WHERE active=1").fetchone()
        else:
            still_active = conn.execute(
                "SELECT id FROM profiles WHERE active=1 AND owner=?",
                (owner_name,)).fetchone()
        if still_active is None:                     # never leave zero active
            if owner_name is None:
                first = conn.execute("SELECT id FROM profiles ORDER BY id"
                                     " LIMIT 1").fetchone()
            else:
                first = conn.execute(
                    "SELECT id FROM profiles WHERE owner=? ORDER BY id LIMIT 1",
                    (owner_name,)).fetchone()
            if first:
                conn.execute("UPDATE profiles SET active=1 WHERE id=?",
                             (first["id"],))
    return True


def activate_profile(profile_id: int, owner: str | None = None) -> dict | None:
    """Make one profile current. Returns it, or None if the id is not visible.

    Scoped activation changes only that user's marker and deliberately does not
    call ``apply()``: the module-level config is shared by every request.
    """
    owner_name = _normalise_username(owner) if owner is not None else None
    with _connect() as conn:
        if owner_name is None:
            if conn.execute("SELECT id FROM profiles WHERE id=?",
                            (profile_id,)).fetchone() is None:
                return None
            conn.execute("UPDATE profiles SET active=0")
            conn.execute("UPDATE profiles SET active=1 WHERE id=?", (profile_id,))
            row = conn.execute("SELECT * FROM profiles WHERE id=?",
                               (profile_id,)).fetchone()
        else:
            if conn.execute(
                    "SELECT id FROM profiles WHERE id=? AND owner=?",
                    (profile_id, owner_name)).fetchone() is None:
                return None
            conn.execute("UPDATE profiles SET active=0 WHERE owner=?",
                         (owner_name,))
            conn.execute("UPDATE profiles SET active=1 WHERE id=? AND owner=?",
                         (profile_id, owner_name))
            row = conn.execute(
                "SELECT * FROM profiles WHERE id=? AND owner=?",
                (profile_id, owner_name)).fetchone()
    profile = _row_profile(row)
    if owner_name is None:
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


# The settings store is only as private as the database file; make the intent
# explicit for anyone reading `ls -l`.
__all__ = [name for name in dir() if not name.startswith("_")]

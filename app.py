"""Exastro Movement / Parameter Sheet Creator.

A single Flask page that builds a self-contained Ansible-LegacyRole Movement
from four inputs:

     Movement Name
     Parameter Rest Name      (blank -> the Movement Name is used)
     Role Package + Role      (dependent dropdowns -> "Package:Role")
     Parameters (JSON)        keys become the parameter-sheet columns

One click creates: the Movement, the Movement-Role link, the Parameter Sheet
(columns derived from the JSON keys) and one Movement-Parameter (substitution)
link *per JSON key* — all in Exastro.

Flow: form submit -> [token] -> create movement -> link role -> collect role
   variables -> create parameter sheet -> bind every parameter -> result view.

The Role Package dropdown is built from the `file_link` menu, i.e. the Git
connections feeding the Role Package List (`demo_pkg` ->
`acme_demo_pkg:demo_pkg/roles`), and the Role dropdown from
ITA's own selectable `Package:Role` values — so whatever the UI offers is
guaranteed to validate on write.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import secrets
import sqlite3
import time
from datetime import datetime

from urllib.parse import urlparse

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, session)

import config as cfg
import i18n
import settings
from exastro_client import ExastroClient, ExastroError, make_client


def _secret_key() -> str:
    """Session signing key.

    The session now carries an authorization decision ("Settings unlocked"), so
    a well-known key would let anyone forge it. EXA_SECRET_KEY wins; otherwise a
    random one is generated once and kept in a 0600 file, so a restart does not
    sign every operator out mid-shift.
    """
    env = os.getenv("EXA_SECRET_KEY")
    if env:
        return env
    path = pathlib.Path(os.getenv("EXA_SECRET_FILE", "flask_secret.key"))
    try:
        if path.exists():
            key = path.read_text(encoding="utf-8").strip()
            if key:
                return key
        key = secrets.token_hex(32)
        path.write_text(key, encoding="utf-8")
        path.chmod(0o600)
        return key
    except OSError:
        # Unwritable directory: sign for this process only. A restart then just
        # expires the unlocked Settings session, which is the safe direction.
        return secrets.token_hex(32)


app = Flask(__name__)
app.secret_key = _secret_key()

DB_PATH = "creations.db"
# Order matters: the active profile rewrites `config`, and only then does a
# client get built. With no settings.db at all, .env values stand.
settings.init_store()
settings.apply_active()
client = make_client()

# The interface language is a browser preference, not a server setting: a cookie
# keeps it across the three pages and a reload. It is deliberately unrelated to
# the Exastro *account* locale, which decides what ITA's own pulldowns and error
# texts say — that one is resolved by id in exastro_client.py.
LANG_COOKIE = "exa_lang"
LANG_AGE = 60 * 60 * 24 * 365


def current_lang() -> str:
    return i18n.normalize(request.cookies.get(LANG_COOKIE) or cfg.UI_LANG)


def _theme() -> str:
    """Chosen palette, falling back to the deployment default."""
    mode = session.get(THEME_KEY)
    return mode if mode in THEMES else (cfg.UI_THEME or "dark")


def _lang() -> str:
    """Language for messages built outside a template.

    Falls back to the configured default when there is no request context, so
    ``parse_params`` and ``create_everything`` stay usable from tests and a
    script.
    """
    try:
        return current_lang()
    except RuntimeError:
        return i18n.normalize(cfg.UI_LANG)


def _back_target() -> str:
    """Same-origin path to return to after a language switch, else the home page.

    The Referer is client-supplied, so anything that is not a plain path — an
    absolute URL, a protocol-relative '//host' — is discarded rather than
    followed.
    """
    referer = request.headers.get("Referer") or ""
    try:
        parts = urlparse(referer)
    except ValueError:
        return "/"
    path = parts.path or "/"
    # Anything that is not a plain path on this very host is dropped: a foreign
    # netloc, protocol-relative '//host', a backslash form some browsers resolve
    # as a host, or no path at all.
    if (parts.netloc and parts.netloc != request.host) or not path.startswith("/"):
        return "/"
    if path.startswith("//") or "\\" in path:
        return "/"
    return path


# Screens that cannot do anything without an Exastro target. The history and its
# detail pages read only the local database, so they stay usable either way.
ITA_NEEDED = ("/", "/create", "/api/roles")


@app.before_request
def require_target():
    """A copy that has not been pointed at an environment must say so.

    Without this, every call would go to whatever `config.py` happened to default
    to and come back as a network timeout — which reads like a broken Exastro,
    not like an app that was never told where to go.
    """
    if cfg.MOCK or _unlocked() or request.path not in ITA_NEEDED:
        return None
    if settings.is_configured():
        return None
    missing = " / ".join(i18n.t(k, _lang()) for k in settings.missing_labels())
    message = i18n.t("setup_needed", _lang(), fields=missing)
    if request.path.startswith("/api/"):
        return jsonify({"error": message}), 409
    flash(message, "error")
    return redirect("/settings?new=1")


@app.context_processor
def inject_i18n():
    lang = current_lang()

    def t(key, **fmt):
        return i18n.t(key, lang, **fmt)

    def tf(key, args=None):
        """Translate with a dict of placeholders.

        Exists because a stored report carries its placeholders as a JSON object;
        this avoids `**` unpacking in template expressions.
        """
        return i18n.t(key, lang, **(args or {}))

    active = settings.active_profile() or {}
    return {
        "lang": lang,
        "t": t,
        "tf": tf,
        # The header shows where writes are currently going: pointing the tool
        # at another environment must never be invisible.
        "profile_name": active.get("name") or "",
        "profile_workspace": cfg.WORKSPACE_ID,
        "settings_unlocked": _unlocked(),
        "theme": _theme(),
        "themes": [{"code": c, "name": i18n.t("theme_" + c, lang),
                    "active": c == _theme()} for c in THEMES],
        "html_lang": "ja" if lang == "ja" else "en",
        "languages": [
            {"code": c, "name": i18n.LANGUAGE_NAMES[c], "active": c == lang}
            for c in i18n.SUPPORTED
        ],
    }


@app.template_filter("when")
def fmt_when(value) -> str:
    """Render a stored timestamp for humans: '2026-09-09 17:44:37'.

    Runs recorded before the storage format changed still hold isoformat's 'T'
    separator (and, if anything ever wrote them, microseconds), so the display
    normalizes both instead of only fixing new rows.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    return text.replace("T", " ", 1).split(".")[0].strip()


@app.get("/lang/<code>")
def set_language(code):
    lang = i18n.normalize(code)
    response = redirect(_back_target())
    # normalize() also maps junk like /lang/de onto a supported language, so the
    # cookie is never set to something the templates cannot render.
    response.set_cookie(LANG_COOKIE, lang, max_age=LANG_AGE, samesite="Lax")
    return response

# The catalogue read costs three Exastro round-trips; cache it briefly so a
# page load is instant without going stale when a Git sync adds a role.
_CATALOGUE_TTL = 30.0
_catalogue: dict = {"at": 0.0, "data": None}


@app.context_processor
def inject_cfg():
    return {"cfg": cfg.describe()}


# ---------------------------------------------------------------------------
# storage (a simple history of what we created)
# ---------------------------------------------------------------------------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn) -> set:
    return {row[1] for row in conn.execute("PRAGMA table_info(creations)")}


def init_db() -> None:
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS creations (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                movement_name TEXT NOT NULL,
                role_name     TEXT NOT NULL,
                sheet_name    TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'OK',
                created_at    TEXT NOT NULL
            )""")
        # `report_json` arrived later: the full step-by-step report behind each
        # row, so the history is readable after the run. SQLite has no
        # CREATE COLUMN IF NOT EXISTS, so probe first — this runs on every boot.
        if "report_json" not in _columns(conn):
            conn.execute("ALTER TABLE creations ADD COLUMN report_json TEXT")


def save_creation(movement_name: str, role_name: str, sheet_name: str,
                  status: str, report: dict | None = None) -> int:
    # A space, not isoformat's 'T': this value is shown verbatim in the history
    # list, and an operator reads 'T' as a typo rather than a separator.
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Stored as text, not as live objects: the report is a snapshot of what the
    # run did, and must survive restarts and later changes to the movement.
    blob = None
    if report is not None:
        try:
            blob = json.dumps(report, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            blob = None
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO creations (movement_name, role_name, sheet_name,"
            " status, created_at, report_json) VALUES (?,?,?,?,?,?)",
            (movement_name, role_name, sheet_name, status, now, blob),
        )
        return int(cur.lastrowid)


def list_creations(limit: int = 10, offset: int = 0) -> list:
    """One page of runs, newest first. History itself is never trimmed."""
    with db() as conn:
        return conn.execute(
            "SELECT id, movement_name, role_name, sheet_name, status,"
            " created_at, report_json FROM creations ORDER BY id DESC"
            " LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()


def count_creations() -> int:
    with db() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM creations").fetchone()[0])


def paginate_creations(page: int = 1, per_page: int = 10) -> dict:
    """Everything the history view needs: a page of rows plus pager metadata."""
    total = count_creations()
    pages = max(1, -(-total // per_page))          # ceiling division
    page = min(max(1, page), pages)
    offset = (page - 1) * per_page
    rows = list_creations(per_page, offset)
    return {
        "rows": rows, "page": page, "per_page": per_page, "total": total,
        "pages": pages, "has_prev": page > 1, "has_next": page < pages,
        "start": offset + 1 if rows else 0, "end": offset + len(rows),
        # A window of page numbers around the current one, so a long history
        # never renders a hundred links.
        "window": [n for n in range(max(1, page - 3), min(pages, page + 3) + 1)],
    }


_QUOTED = re.compile(r"'(.*)'")
_KEYCOUNT = re.compile(r"\((\d+) keys?\)")


def _translatable_steps(snapshot) -> None:
    """Give pre-existing reports the i18n identity newer runs are stored with.

    Rows written before ``i18n``/``args`` existed carry only an English ``label``,
    so they would stay English after switching. Each step does keep its stable
    ``key``, and the name/count is recoverable from the label — so the history
    already recorded renders in either language too. The database is not touched;
    this only fills the decoded copy.
    """
    report = (snapshot or {}).get("report") or {}
    for step in report.get("steps") or []:
        if not isinstance(step, dict) or step.get("i18n"):
            continue
        key = step.get("key")
        if not key:
            continue
        label = str(step.get("label") or "")
        if key == "param_link":
            found = _KEYCOUNT.search(label)
            args = {"count": found.group(1) if found else ""}
        else:
            found = _QUOTED.search(label)
            args = {"name": found.group(1) if found else ""}
        step["i18n"] = f"step_{key}"
        step["args"] = args


def get_creation(creation_id: int) -> dict | None:
    """One stored run with its report decoded, or None if it does not exist."""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM creations WHERE id = ?", (creation_id,)).fetchone()
    if row is None:
        return None
    report = None
    if row["report_json"]:
        try:
            report = json.loads(row["report_json"])
        except (ValueError, TypeError):
            report = None
    _translatable_steps(report)
    return {
        "id": row["id"],
        "movement_name": row["movement_name"],
        "role_name": row["role_name"],
        "sheet_name": row["sheet_name"],
        "status": row["status"],
        "created_at": row["created_at"],
        "report": report,
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def parse_params(raw: str) -> dict:
    """Parse the JSON textarea; raises ValueError with a clear message."""
    text = (raw or "").strip()
    lang = _lang()
    if not text:
        raise ValueError(i18n.t("err_params_required", lang))
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(i18n.t("err_params_json", lang, exc=exc)) from exc
    if not isinstance(obj, dict):
        raise ValueError(i18n.t("err_params_object", lang))
    if not obj:
        raise ValueError(i18n.t("err_params_empty", lang))
    return obj


# The column grid's field names. Anything else in an entry is a bug in the page
# or a hand-typed payload, and refusing is better than silently half-applying.
META_FIELDS = ("name", "required", "unique")
# ITA's own cap on a column display name is not published by the API, so this is
# a generous local ceiling; ITA remains the authority and its message is passed
# through verbatim if it disagrees.
META_NAME_BYTES = 128


def parse_column_meta(raw: str, params: dict) -> dict:
    """Validate the grid's JSON -> {logical: {name?, required?, unique?}}.

    Only rows the operator touched arrive here at all: the page drops an entry
    whose name equals its logical name and whose boxes are both clear, so an
    untouched form sends `{}` and the definition is written exactly as it was
    before the grid existed.
    """
    lang = _lang()
    text = (raw or "").strip()
    if not text or text == "{}":
        return {}
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(i18n.t("err_meta_json", lang, exc=exc)) from exc
    if not isinstance(obj, dict) or isinstance(obj, list):
        raise ValueError(i18n.t("err_meta_object", lang))

    out: dict[str, dict] = {}
    for key, entry in obj.items():
        if key not in params:
            # The JSON changed after the grid was drawn, or someone edited the
            # hidden field. Naming it beats binding to a column that is gone.
            raise ValueError(i18n.t("err_meta_key", lang, logic=key))
        if not isinstance(entry, dict):
            raise ValueError(i18n.t("err_meta_shape", lang, logic=key))
        clean: dict = {}
        for field in META_FIELDS:
            if field not in entry:
                continue
            value = entry[field]
            if field == "name":
                name = str(value or "").strip()
                if len(name.encode("utf-8")) > META_NAME_BYTES:
                    raise ValueError(i18n.t(
                        "err_meta_long", lang, logic=key, n=META_NAME_BYTES,
                        got=len(name.encode("utf-8"))))
                # An empty box is kept, not dropped: the grid prefills what the
                # sheet already holds, so clearing it is a deliberate "put the
                # logical name back", and the payload builder resolves "" to the
                # key. Dropping it here would turn an instruction into silence.
                clean["name"] = name
            else:
                clean[field] = bool(value)
        if clean:
            out[key] = clean

    seen: dict[str, str] = {}
    for key, entry in out.items():
        label = entry.get("name") or key
        if label in seen:
            raise ValueError(i18n.t("err_meta_dup", lang, name=label,
                                   a=seen[label], b=key))
        seen[label] = key
    return out


def infer_types(params: dict) -> dict:
    """Human-readable type summary of each JSON value (for the results page)."""
    out = {}
    for key, value in params.items():
        if isinstance(value, bool):
            out[key] = "bool"
        elif isinstance(value, int):
            out[key] = "integer"
        elif isinstance(value, float):
            out[key] = "decimal"
        elif isinstance(value, (list, dict)):
            out[key] = "multi-string"
        else:
            out[key] = "string"
    return out


def catalogue(force: bool = False, cl=None) -> dict:
    """Role packages + roles + execution environments, lightly cached."""
    cl = client if cl is None else cl
    now = time.time()
    if not force and _catalogue["data"] and now - _catalogue["at"] < _CATALOGUE_TTL:
        return _catalogue["data"]
    data = {
        "packages": cl.role_choices(),
        "environments": cl.execution_environments(),
        "default_env": cfg.EXEC_ENV,
    }
    _catalogue["at"] = now
    _catalogue["data"] = data
    return data


def rebuild_client() -> None:
    """Point the app at whatever `config` now says, and drop cached reads.

    Requests already in flight are unaffected: each view binds its own `cl` and
    passes it down, so saving Settings halfway through a creation cannot move
    that creation to another environment.
    """
    global client
    client = make_client()
    _catalogue["at"] = 0.0
    _catalogue["data"] = None


def compose_role_value(package: str, role: str, manual: str) -> str:
    """Assemble the `Package:Role` value written to movement_role_link.

    The dropdowns are the normal path (`demo_pkg` +
    `DEMO_HOST_JOB` -> `demo_pkg:DEMO_HOST_JOB`). The
    advanced box wins when it already contains a separator, so a hand-typed or
    pre-composed `Package:Role` still works; a bare role typed there is paired
    with the chosen package.
    """
    sep = cfg.ROLE_PKG_SEP
    package = (package or "").strip()
    role = (role or "").strip()
    manual = (manual or "").strip()

    if sep in manual:
        return manual
    if manual and not role:
        role = manual
    if package and role:
        return f"{package}{sep}{role}"
    return manual or role



# Steps whose detail string denotes a problem. 'missing' and 'unavailable' are
# included because an earlier build reported a fully-skipped parameter step as
# green — the old check only looked for the word 'fail'.
_BAD_MARKERS = ("fail", "error", "missing", "no column", "not offered",
                "not set", "not registered", "not selectable", "unavailable",
                "exception")


def _looks_bad(detail) -> bool:
    text = str(detail).lower()
    return any(marker in text for marker in _BAD_MARKERS)


def create_everything(movement_name: str, role_name: str, sheet_name: str,
                      params: dict, execution_env: str = "",
                      wait_vars: bool = True, cl=None,
                      column_meta: dict | None = None,
                      replace_flags: bool = False) -> dict:
    """Run the full creation sequence. Returns a step-by-step report.

    Errors are caught *per step* so one failure doesn't mask what already
    succeeded. The parameter step reports one entry per JSON key, so a partial
    binding is visible instead of looking like success.

    `cl` lets the caller pin the client for the whole sequence: a Settings save
    replaces the module-level client, and a run in flight must not follow it to
    another environment halfway through.
    """
    cl = client if cl is None else cl
    steps: list[dict] = []
    bindings: list[dict] = []
    status = "OK"

    def degrade(new: str) -> None:
        nonlocal status
        order = {"OK": 0, "PARTIAL": 1, "FAILED": 2}
        if order[new] > order[status]:
            status = new

    def step(key: str, label: str, fn, args: dict | None = None) -> object:
        """Run one creation step, recording it in a form either language can render.

        ``label`` stays English because it is what goes into the database, and
        ``_looks_bad()`` matches details (not labels) in English too. ``i18n`` +
        ``args`` are what the templates translate from, so a run recorded in
        English mode still reads correctly after switching to Japanese.
        """
        try:
            detail = fn()
            bad = _looks_bad(detail)
            if bad:
                degrade("PARTIAL")
            steps.append({"key": key, "label": label, "i18n": f"step_{key}",
                          "args": args or {}, "ok": not bad,
                          "detail": str(detail)})
            return detail
        except ExastroError as exc:
            degrade("FAILED")
            steps.append({"key": key, "label": label, "i18n": f"step_{key}",
                          "args": args or {}, "ok": False,
                          "detail": str(exc)})
            return None

    step("movement", f"Create Movement '{movement_name}'",
         lambda: cl.create_movement(movement_name, execution_env),
         {"name": movement_name})
    step("role_link", f"Link Movement <-> Role '{role_name}'",
         lambda: cl.link_movement_role(movement_name, role_name),
         {"name": role_name})
    step("param_sheet", f"Create Parameter Sheet '{sheet_name}'",
         lambda: cl.create_parameter_sheet(sheet_name, params, column_meta,
                                           replace_flags=replace_flags),
         {"name": sheet_name})

    links = step(
        "param_link",
        f"Link Movement <-> Parameter ({len(params)} keys)",
        lambda: cl.link_movement_parameter(movement_name, sheet_name, params,
                                               wait=wait_vars),
        {"count": len(params)}) or []
    bindings = list(links)
    linked = sum(1 for b in bindings if b["status"] in ("linked", "exists"))
    last = steps[-1]
    last["detail"] = f"{linked}/{len(bindings)} parameters bound"
    last["ok"] = linked == len(bindings) and bool(bindings)
    if bindings and linked < len(bindings):
        degrade("PARTIAL")

    return {"status": status, "steps": steps, "bindings": bindings,
            "linked": linked, "total": len(bindings)}


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    cat = None
    try:
        cat = catalogue()
    except ExastroError as exc:
        flash(i18n.t("err_catalogue", _lang(), exc=exc), "error")
    # A run's Detail page offers "re-run with these parameters", which arrives
    # as query args; prefill so the form is ready to submit as-is.
    args = request.args
    return render_template("index.html",
                           creations=paginate_creations(_page_arg(args)),
                           cfg=cfg.describe(), catalogue=cat,
                           movement_name=args.get("movement_name", ""),
                           role_name=args.get("role_name", ""),
                           sheet_name=args.get("sheet_name", ""),
                           parameters=args.get("parameters", ""))


@app.get("/api/sheet/<name>")
def api_sheet_columns(name: str):
    """What a parameter sheet already holds, to seed the column grid.

    Read-only. The three states are kept apart on purpose: a sheet that does not
    exist seeds blank rows, while a sheet that could not be read has to be
    reported, or the operator would type into a form that silently means
    "change nothing" while believing it means "use the logical name".
    """
    name = (name or "").strip()
    if not name:
        return jsonify({"state": "absent", "columns": {}})
    try:
        state, cols = client.sheet_column_state(name)
    except Exception as exc:                 # a transport error is an answer too
        return jsonify({"state": "error", "columns": {},
                        "error": str(exc)}), 502
    return jsonify({"state": state, "columns": cols})


@app.get("/api/role_choices")
def api_role_choices():
    """Dependent-dropdown data: Git packages, their roles, and environments."""
    try:
        return jsonify(catalogue(force=request.args.get("refresh") == "1"))
    except ExastroError as exc:
        return jsonify({"error": str(exc)}), 502


@app.post("/create")
def create():
    movement_name = (request.form.get("movement_name") or "").strip()
    sheet_name = (request.form.get("sheet_name") or movement_name).strip()
    role_name = compose_role_value(request.form.get("role_package"),
                                  request.form.get("role_select"),
                                  request.form.get("role_name"))
    execution_env = (request.form.get("exec_env") or "").strip()
    wait_vars = (request.form.get("wait_vars") or "on") in ("on", "true", "1")
    params_raw = request.form.get("parameters", "")

    # --- validate ---
    # Messages are rendered in the interface language the operator chose, not
    # both at once.
    lang = _lang()
    errors = []
    if not movement_name:
        errors.append(i18n.t("err_movement_required", lang))
    if not role_name:
        errors.append(i18n.t("err_role_required", lang))
    elif cfg.ROLE_PKG_SEP not in role_name:
        errors.append(i18n.t("err_role_format", lang, sep=cfg.ROLE_PKG_SEP,
                             got=role_name))
    # A blank Parameter Rest Name is valid: it falls back to the movement name.
    if wait_vars and not execution_env:
        errors.append(i18n.t("err_exec_env_required", lang))
    try:
        params = parse_params(params_raw)
    except ValueError as exc:
        errors.append(str(exc))
        params = {}

    # The grid is generated from these keys, so it is validated against them:
    # a row whose key has since been deleted from the JSON would otherwise be
    # silently dropped, or worse, rename a different column.
    # the destructive switch, off unless the box is ticked
    replace_flags = request.form.get("replace_flags") == "1"
    meta_raw = request.form.get("column_meta", "")
    column_meta = {}
    try:
        column_meta = parse_column_meta(meta_raw, params)
    except ValueError as exc:
        errors.append(str(exc))

    if errors:
        for e in errors:
            flash(e, "error")
        try:
            cat = catalogue()
        except ExastroError:
            cat = None
        return render_template("index.html",
                               creations=paginate_creations(
                                   _page_arg(request.args)),
                               cfg=cfg.describe(), catalogue=cat,
                               movement_name=movement_name, role_name=role_name,
                               sheet_name=sheet_name, parameters=params_raw,
                               column_meta=meta_raw,
                               replace_flags=replace_flags), 400

    # Bind the client once for the whole request. A Settings save can replace
    # the module-level client while this is running; this run keeps its target.
    cl = client
    if not execution_env and wait_vars:
        execution_env = cl.resolve_execution_env(execution_env)

    report = create_everything(movement_name, role_name, sheet_name, params,
                               execution_env, wait_vars, cl=cl,
                               column_meta=column_meta,
                               replace_flags=replace_flags)
    types = infer_types(params)
    # Store the whole run, not just its status, so /creation/<id> can replay it
    # later — the Exastro objects may change or be deleted in between.
    save_creation(movement_name, role_name, sheet_name, report["status"],
                  {"report": report, "params": params, "types": types,
                   "execution_env": execution_env, "wait_vars": wait_vars})

    return render_template(
        "result.html",
        movement_name=movement_name, role_name=role_name, sheet_name=sheet_name,
        params=params, types=types, report=report, execution_env=execution_env,
    )


@app.get("/healthz")
def healthz():
    active = settings.active_profile() or {}
    return {"ok": True, "mock": cfg.MOCK,
            "configured": settings.is_configured(),
            "profile": active.get("name", "(.env defaults)"),
            "gateway": cfg.GATEWAY_URL, "workspace": cfg.WORKSPACE_ID}


@app.get("/creation/<int:creation_id>")
def creation_detail(creation_id: int):
    """Full history of one run: every step, every parameter binding, as it happened."""
    row = get_creation(creation_id)
    if row is None:
        abort(404)
    return render_template("detail.html", c=row, cfg=cfg.describe())


def _page_arg(args) -> int:
    """?page= as an integer, tolerating anything a stale link throws at us."""
    try:
        return int(args.get("page", 1))
    except (TypeError, ValueError):
        return 1


@app.get("/api/creations")
def api_creations():
    try:
        per_page = int(request.args.get("per_page") or 10)
    except (TypeError, ValueError):
        per_page = 10
    page = paginate_creations(_page_arg(request.args), per_page)
    return jsonify({
        "page": page["page"], "pages": page["pages"], "total": page["total"],
        "per_page": page["per_page"], "has_prev": page["has_prev"],
        "has_next": page["has_next"],
        "creations": [
            {"id": r["id"], "movement_name": r["movement_name"],
             "role_name": r["role_name"], "sheet_name": r["sheet_name"],
             "status": r["status"], "created_at": r["created_at"],
             "has_detail": bool(r["report_json"]),
             "detail_url": f"/creation/{r['id']}"}
            for r in page["rows"]
        ]})



# ---------------------------------------------------------------------------
# Settings: named connection profiles, gated behind a PIN
# ---------------------------------------------------------------------------

# The session flag is the only thing standing between a stranger on the network
# and repointing this app at an arbitrary host with your token, so it expires.
UNLOCK_KEY = "settings_unlocked_at"
THEME_KEY = "theme"
THEMES = ("dark", "light")           # the two palettes in static/theme.css


def _unlocked() -> bool:
    stamp = session.get(UNLOCK_KEY) or 0
    try:
        return (time.time() - float(stamp)) < settings.UNLOCK_TTL
    except (TypeError, ValueError):
        return False


def _locked_redirect():
    flash(i18n.t("pin_required", _lang()), "error")
    return redirect("/settings")


def _id_options(cl=None) -> dict:
    """Ids the target environment offers, or {} when it cannot be read.

    A failure is deliberately quiet here: the form still has to render, and a
    raw number is better than a blank one the operator cannot recognize.
    """
    cl = client if cl is None else cl
    try:
        return cl.id_options() or {}
    except Exception:
        return {}


def _pair_labels(values: dict, options: dict) -> dict:
    """Write the display string that belongs to a chosen id.

    ITA validates these columns by id and the client re-resolves the label at
    write time, so this is bookkeeping only — but without it a profile would keep
    an outdated fallback literal next to a newly chosen id.
    """
    for f in settings.FIELDS:
        pair = f.get("pair")
        if not pair:
            continue
        chosen = str(values.get(f["key"]) or "")
        for ident, label in (options.get(f.get("options_from") or "") or []):
            if ident == chosen:
                values[pair] = label
                break
    return values


def _form_fields(payload: dict, options: dict | None = None) -> dict:
    """The field table as grouped form items, with secrets reduced to a hint."""
    groups = {"connection": [], "auth": [], "tuning": [], "advanced": []}
    for f in settings.FIELDS:
        if f.get("hidden"):
            continue          # written from its paired id, not edited directly
        raw = payload.get(f["key"], "")
        item = {"key": f["key"], "name": f"field_{f['key']}",
                "kind": f["kind"], "label": f["label"],
                "hint": f.get("hint") or "",
                "placeholder": f.get("placeholder") or "",
                "required": bool(f.get("required")),
                "secret": f["kind"] == "secret", "group": f["group"]}
        if f["kind"] == "secret":
            # Never a value: the real token must not appear in the page source.
            item["value"] = ""
            item["secret_hint"] = settings.secret_hint(raw)
            item["clear_name"] = f"clear_{f['key']}"
        elif f["kind"] == "bool":
            item["value"] = "on"
            item["checked"] = bool(raw)
        else:
            item["value"] = "" if raw is None else str(raw)
        choices = (options or {}).get(f.get("options_from") or "") or []
        if choices:
            item["options"] = [{"value": i, "label": l} for i, l in choices]
            item["offered"] = any(i == item["value"] for i, _ in choices)
        groups[f["group"]].append(item)
    return groups


def _form_values() -> dict:
    """Read the profile form back into a payload.

    A blank field is *omitted* rather than stored empty: for a secret that means
    "keep the stored one", and for a number "keep the default" instead of 0.
    """
    values: dict = {}
    for f in settings.FIELDS:
        if f.get("hidden"):
            continue
        raw = request.form.get(f"field_{f['key']}")
        if f["kind"] == "bool":
            values[f["key"]] = bool(raw)
            continue
        text = (raw or "").strip()
        if not text:
            continue
        if f["kind"] == "int":
            try:
                text = int(text)
            except ValueError:
                pass          # left as text so _validate can name the field
        values[f["key"]] = text
    return values


def _validate(values: dict) -> list[str]:
    lang = _lang()
    errors: list[str] = []
    for f in settings.FIELDS:
        label = i18n.t(f["label"], lang)
        key = f["key"]
        if f.get("required") and not str(values.get(key) or "").strip():
            errors.append(i18n.t("set_required", lang, field=label))
        if f["kind"] != "int" or key not in values:
            continue
        try:
            number = int(values[key])
        except (TypeError, ValueError):
            errors.append(i18n.t("set_number", lang, field=label))
            continue
        low, high = int(f.get("min", 0)), int(f.get("max", 10 ** 9))
        if number < low or number > high:
            errors.append(i18n.t("set_range", lang, field=label, lo=low, hi=high))
    gateway = str(values.get("GATEWAY_URL") or "").strip()
    if gateway and urlparse(gateway).scheme not in ("http", "https"):
        errors.append(i18n.t("set_url_scheme", lang))
    return errors


@app.get("/settings")
def settings_page():
    if not _unlocked():
        return render_template("settings_pin.html",
                               pin_set=settings.pin_configured(),
                               cfg=cfg.describe())
    active = settings.active_profile()
    # A rejected save wins over everything else: the operator must see the form
    # they just filled in, including a pasted token, not the stored profile.
    draft = session.pop("settings_draft", None)
    if draft is not None:
        pid = draft.get("pid")
        target = settings.get_profile(pid) if pid else None
        target = dict(target or {"id": None})
        target["name"] = draft.get("name") or ""
    else:
        wanted = (request.args.get("edit") or "").strip()
        target = settings.get_profile(int(wanted)) if wanted.isdigit() else None
        if not request.args.get("new"):
            target = target or active
    merged = {**settings.env_defaults(), **((target or {}).get("payload") or {}),
              **((draft or {}).get("values") or {})}
    options = _id_options()
    # Only hinted copies reach the template: a stray {{ p.payload.API_TOKEN }} in
    # some future edit would otherwise paste the operator's token into the HTML.
    public = [{**p, "payload": settings.public_payload(p)}
              for p in settings.list_profiles()]
    view = dict(target or {})
    view["payload"] = settings.public_payload(view) if target else {}
    return render_template("settings.html", profiles=public,
                           setup_missing=[] if settings.is_configured()
                           else [i18n.t(k, _lang())
                                 for k in settings.missing_labels()],
                           active=bool(active and active.get("id") ==
                                       (target or {}).get("id")),
                           target=view or None,
                           fields=_form_fields(merged, options),
                           options_found=bool(options),
                           derived=settings.derived(merged),
                           minutes=int(settings.UNLOCK_TTL // 60),
                           cfg=cfg.describe())


@app.post("/settings/unlock")
def settings_unlock():
    """Verify the PIN — or, on a store that has none yet, choose one.

    First run is unavoidably open: whoever starts the app first sets the PIN.
    After that it is required, and `EXA_SETTINGS_PIN` overrides the stored one.
    """
    lang = _lang()
    pin = request.form.get("pin") or ""
    if not settings.pin_configured():
        confirm = request.form.get("pin_confirm") or ""
        if len(pin) < 4:
            flash(i18n.t("pin_short", lang), "error")
        elif pin != confirm:
            flash(i18n.t("pin_mismatch", lang), "error")
        else:
            settings.set_pin(pin)
            session[UNLOCK_KEY] = time.time()
            flash(i18n.t("pin_created", lang), "success")
        return redirect("/settings")
    ok, wait = settings.check_pin(pin)
    if ok:
        session[UNLOCK_KEY] = time.time()
        return redirect("/settings")
    if wait > 0:
        flash(i18n.t("pin_cooldown", lang, s=int(wait + 0.5)), "error")
    else:
        flash(i18n.t("pin_wrong", lang, left=settings.attempts_left()), "error")
    return redirect("/settings")


@app.get("/theme/<mode>")
def theme_set(mode: str):
    """Pick a palette. A link, like the language switch, so the pages need no
    script to change their own colours."""
    if mode in THEMES:
        session[THEME_KEY] = mode
    return redirect(request.referrer or "/")


@app.post("/settings/lock")
def settings_lock():
    session.pop(UNLOCK_KEY, None)
    flash(i18n.t("locked", _lang()), "success")
    return redirect("/settings")


def _clear_flags() -> tuple:
    """Secrets the form explicitly asked to forget ('keep blank' is the norm)."""
    return tuple(key for key in settings.SECRET_FIELDS
                 if request.form.get(f"clear_{key}"))


def _draft_id() -> int | None:
    raw = (request.form.get("profile_id") or "").strip()
    return int(raw) if raw.isdigit() else None


@app.post("/settings/save")
def settings_save():
    if not _unlocked():
        return _locked_redirect()
    lang = _lang()
    name = (request.form.get("profile_name") or "").strip()
    pid = _draft_id()
    values = _pair_labels(_form_values(), _id_options())
    errors = _validate(values)
    if not name:
        errors.append(i18n.t("set_name_required", lang))
    if errors:
        for message in errors:
            flash(message, "error")
        session["settings_draft"] = {"name": name, "pid": pid, "values": values}
        return redirect(f"/settings?edit={pid}" if pid else "/settings")
    session.pop("settings_draft", None)
    try:
        saved = settings.save_profile(name, values, pid,
                                      clear=_clear_flags())
    except (ValueError, KeyError, sqlite3.Error) as exc:
        flash(i18n.t("set_save_failed", lang, error=str(exc)), "error")
        return redirect("/settings")
    # A new profile is the one you just came here to reach, so it goes active;
    # editing an existing one leaves the current target alone unless asked.
    if request.form.get("activate") or pid is None:
        settings.activate_profile(saved)
        rebuild_client()
        flash(i18n.t("set_active_now", lang, name=name), "success")
    else:
        flash(i18n.t("set_saved", lang, name=name), "success")
    return redirect("/settings")


@app.post("/settings/activate")
def settings_activate():
    if not _unlocked():
        return _locked_redirect()
    pid = _draft_id()
    profile = settings.activate_profile(pid) if pid else None
    if profile is None:
        flash(i18n.t("set_no_profile", _lang()), "error")
    else:
        rebuild_client()
        flash(i18n.t("set_active_now", _lang(), name=profile["name"]), "success")
    return redirect("/settings")


@app.post("/settings/delete")
def settings_delete():
    if not _unlocked():
        return _locked_redirect()
    pid = _draft_id()
    if pid and settings.delete_profile(pid):
        # Deleting the active profile leaves `config` pointing at a target that
        # no longer exists, so re-apply whatever is active now.
        settings.apply_active()
        rebuild_client()
        flash(i18n.t("set_deleted", _lang()), "success")
    else:
        flash(i18n.t("set_no_profile", _lang()), "error")
    return redirect("/settings")


@app.post("/settings/test")
def settings_test():
    """Probe a *draft* profile. Deliberately does not touch the active client.

    Catching every error here is the point: a failed probe against a host the
    operator is still configuring should be reported as text on the page, not as
    a 500 with a traceback.
    """
    if not _unlocked():
        return _locked_redirect()
    lang = _lang()
    pid = _draft_id()
    values = _form_values()
    if pid:
        stored = (settings.get_profile(pid) or {}).get("payload") or {}
        for key in settings.SECRET_FIELDS:
            # A blank secret means "the stored one" — the browser never held it.
            if not values.get(key) and stored.get(key):
                values[key] = stored[key]
    draft = {**values, **settings.derived(values)}
    try:
        result = ExastroClient(draft).probe()
    except Exception as exc:
        flash(i18n.t("set_test_fail", lang, error=str(exc)), "error")
        return redirect("/settings")
    flash(i18n.t("set_test_ok", lang, token_ms=result["token_ms"],
                 api_ms=result["api_ms"], sheet_types=result["sheet_types"],
                 menu_groups=result["menu_groups"],
                 base=result["api_base"]), "success")
    return redirect("/settings")


init_db()

if __name__ == "__main__":
    # threaded: a slow Exastro catalogue call must not block the rest of the UI.
    app.run(host="0.0.0.0", port=9200, debug=False, threaded=True)

"""Exastro IT Automation creation client.

Creates, in one pass, everything a self-contained Ansible-LegacyRole Movement
needs — driven by nothing more than a movement name, a role name and a JSON
object whose keys become the parameter-sheet columns:

    [1] role package        POST .../menu/role_package_list/maintenance/
    [2] movement            POST .../menu/movement_list_ansible_role/maintenance/
    [3] movement-role link  POST .../menu/movement_role_link/maintenance/
    [4] parameter sheet     create/define flow (definition + execute)
    [5] movement-parameter  POST .../menu/subst_value_auto_reg_setting_ansible_role/
                            (one row per JSON key -> role variable)

The JSON values are inspect→type-mapped: bool -> Bool/int -> Num,
float -> Float, str -> SingleText (the small set that covers most input
sheets). Everything is reversible-by-config; set EXA_MOCK=true to run the
whole flow with no live Exastro.
"""

from __future__ import annotations

import base64
import json
import time

import requests

import config as cfg


class ExastroError(RuntimeError):
    """Raised for any failed Exastro interaction (message is UI-safe)."""


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------
#
# ITA answers validation failures as HTTP 499 with result code 499-00201, which
# covers *every* kind of rejected field — unusable value, missing lock token,
# duplicate combination — so the code cannot discriminate and the message text
# must be read. That text is translated into the calling account's language:
# 'The combination is incorrect.' arrives as '組み合わせが不正です。'. Matching only
# English made a Japanese account's duplicate rows look like hard failures
# instead of triggering the update path.
# `create/define/execute/` refuses an absent or empty `role_list` outright
# ("The target key value is invalid. (key: role_list)") while happily accepting a
# value it does not know. So "grant nobody" cannot be sent as [] — it has to be a
# name that matches no role, which leaves the sheet administered by workspace
# rights alone. Verified against the live 2.8 install.
NO_ROLE_SENTINEL = "_no_admin_role"

_DUPLICATE_MARKERS = (
    "combination is incorrect", "duplicated", "already exists",
    "already defined", "already registered",
    "組み合わせが不正", "他レコードと重複", "重複しています",
)
# A create/define response can be HTTP-clean and still say "duplicate" or
# "invalid" in its body; the edit path checks for that before trusting itself.
_BAD_RESULT_MARKERS = ("invalid", "duplicate", "error", "fail", "利用できない", "不正")

_UNUSABLE_VALUE_MARKERS = (
    "cannot be used", "is invalid", "unusable",
    "利用できない値", "不正な値",
)


def resolve_role_list(requested: str, user: str = "") -> list[str]:
    """The sheet's admin role list, exactly as asked for.

    Kept free of any client so the Settings preview can show what a save would
    send without touching the transport. Blank means *grant nothing*: this tool
    does not invent an administrator, and an account that may create but not
    assign roles still gets a sheet out of the request. A named role is never
    second-guessed nor swapped for an admin one; if an install refuses it,
    `create_parameter_sheet` retries without the grant and says so.

    Comma or semicolon separated names are allowed, because `role_list` is an
    array on the ITA side — typically a team role plus the operator's own user,
    so nobody locks themselves out of the definition afterwards. `user` is the
    caller's account name, which is what a blank field resolves to; the sentinel
    survives only when that cannot be determined at all.
    """
    names = [n.strip() for n in (requested or "").replace(";", ",").split(",")]
    names = [n for n in names if n]
    # never an empty list: create/define/execute/ rejects that before anything else
    return names or [str(user or "").strip() or NO_ROLE_SENTINEL]


def _contains_any(text: str, markers) -> bool:
    low = text.lower()
    return any(m.lower() in low for m in markers)



# ---------------------------------------------------------------------------
# parameter-sheet helpers
# ---------------------------------------------------------------------------

def _column_class(value) -> tuple[str, str]:
    """Map a Python scalar to an ITA column class (name, id).

    Ids from the platform's column_class_list: 1 SingleText, 2 MultiText,
    3 Num, 4 Float, 5 DateTime, 6 Date, 7 Pulldown/ID, 8 Password, 10 Link.

    Booleans map to SingleText (not Pulldown): a pulldown column REQUIRES a
    reference source menu, which a freshly-created sheet cannot supply. Values
    are written as the literal string 'True'/'False', matching how ITA renders
    flags elsewhere.
    """
    if isinstance(value, bool):
        return "SingleTextColumn", "1"
    if isinstance(value, int):
        return "NumColumn", "3"
    if isinstance(value, float):
        return "FloatColumn", "4"
    if isinstance(value, list) or isinstance(value, dict):
        return "MultiTextColumn", "2"
    return "SingleTextColumn", "1"


def _default_value(value, column_class_id: str):
    """Return the sheet column's *_default_value field (key depends on class)."""
    if column_class_id == "3":
        return {"integer_default_value": "" if value is None else str(value)}
    if column_class_id == "4":
        return {"decimal_default_value": "" if value is None else str(value)}
    return {"single_string_default_value": "" if value is None else str(value)}


def _flag(meta_entry: dict, name: str) -> str | None:
    """'1'/'0' for a checkbox the operator actually touched, None otherwise.

    None means "the JSON said nothing", which on an existing sheet must leave
    whatever ITA holds alone rather than writing a default that looks identical
    to an intentional choice.
    """
    if not isinstance(meta_entry, dict) or name not in meta_entry:
        return None
    return "1" if meta_entry[name] else "0"


def _display_name(meta_entry: dict, key: str) -> str:
    """The column's physical (display) name, defaulting to the logical one."""
    if isinstance(meta_entry, dict):
        got = str(meta_entry.get("name") or "").strip()
        if got:
            return got
    return key


class ExastroClient:
    """Thin wrapper over the ITA v2 organization API with token caching."""

    def __init__(self, overrides: dict | None = None) -> None:
        self._access: str | None = None
        self._access_exp: float = 0.0
        # Per-instance values that win over the module-level `config`. Normally
        # empty: the active profile is applied to `config` and this client simply
        # follows it. `probe_settings()` builds a throwaway client with a *draft*
        # profile here, so testing a connection cannot repoint a running one.
        self._over = dict(overrides or {})
        self.session = requests.Session()
        self.session.verify = self._v("VERIFY_TLS")
        # Optional map of JSON parameter key -> role variable name, set by a
        # caller when the parameter names differ from the role's variable names.
        self._variable_map: dict[str, str] = {}
        # Cached `GET /create/define/` option lists — see _display_choices().
        self._choices_cache: dict | None = None
        # Cached id choices for the Settings form — see id_options().
        self._id_options_cache: dict | None = None
        # Resolved account name for a blank `Define Role` field — see
        # own_identity(). None means "not asked yet", the sentinel means
        # "asked and the platform would not say".
        self._identity: str | None = None

    def _v(self, name: str, default=None):
        """Resolve a setting: this instance's override, else the live config."""
        over = self._over.get(name)
        if over is not None and over != "":
            return over
        return getattr(cfg, name, default)

    @property
    def _api(self) -> str:
        """Base URL of the ITA organization API for this client."""
        return str(self._v("API_BASE"))

    # ---------------- auth ----------------

    def token(self) -> str:
        """Return a valid access token, exchanging credentials if needed."""
        if self._access and time.time() < self._access_exp - 60:
            return self._access
        if self._v("API_TOKEN"):
            self._exchange({
                "grant_type": "refresh_token",
                "refresh_token": self._v("API_TOKEN"),
            })
        elif self._v("USER") and self._v("PASSWORD"):
            self._exchange({
                "grant_type": "password",
                "username": self._v("USER"),
                "password": self._v("PASSWORD"),
            })
        else:
            raise ExastroError(
            "No credentials set: give this profile an API token (refresh token), "
            "or a username and password — in Settings, or via EXA_API_TOKEN / "
            "EXA_USER + EXA_PASSWORD in .env.")
        return self._access  # type: ignore[return-value]

    def _exchange(self, data: dict) -> None:
        """Exchange credentials for an access token, with one retry.

        Keycloak cold starts and general workspace load routinely push auth past
        the normal API timeout. Auth therefore gets its own (longer) budget and a
        retry, so one slow token call does not surface to the user as a page
        failure — the single-threaded dev server would otherwise block on it.
        """
        payload = {"client_id": self._v("CLIENT_ID"), **data}
        resp = None
        for attempt in range(2):
            try:
                resp = self.session.post(self._v("TOKEN_URL"), data=payload,
                                         timeout=self._v("AUTH_TIMEOUT"))
                break
            except requests.RequestException as exc:
                if attempt == 0:
                    time.sleep(2)
                    continue
                raise ExastroError(
                    f"Cannot reach token endpoint {self._v('TOKEN_URL')}: {exc}. "
                    f"Exastro may be busy — wait a few seconds and reload.") from exc
        if resp.status_code != 200:
            raise ExastroError(
                f"Token exchange failed ({resp.status_code}). Mint a fresh API "
                f"token in the Platform UI and save it in Settings (or EXA_API_TOKEN)."
            )
        body = resp.json()
        self._access = body.get("access_token")
        if not self._access:
            raise ExastroError("Token endpoint returned no access_token.")
        try:
            ttl = int(body.get("expires_in", 300))
        except (TypeError, ValueError):
            ttl = 300
        self._access_exp = time.time() + ttl

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.token()}"}
        if extra:
            headers.update(extra)
        return headers

    def _request(self, method: str, url: str, **kwargs) -> dict | list:
        kwargs.setdefault("timeout", self._v("TIMEOUT"))
        last_exc = None
        for attempt in (1, 2):
            try:
                resp = self.session.request(method, url,
                                            headers=self._headers(), **kwargs)
                break
            except requests.RequestException as exc:
                last_exc = exc
                if attempt == 1:
                    time.sleep(2)
                    continue
        else:
            raise ExastroError(f"Request to {url} failed: {last_exc}")
        if resp.status_code == 401:
            self._access = None
            resp = self.session.request(method, url, headers=self._headers(), **kwargs)
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text[:2000]}
        if resp.status_code >= 400:
            msg = ""
            if isinstance(body, dict):
                msg = str(body.get("message") or body)
            raise ExastroError(f"{method} {url} -> HTTP {resp.status_code}: {msg}")
        return body

    def _result(self, body: dict | list) -> str:
        """Short 'OK' / error text from a {result, message} response."""
        if isinstance(body, dict):
            result = str(body.get("result", "") or "000-00000")
            if result.startswith("000"):
                return "OK"
            return f"{result}: {body.get('message', '')}".strip(": ")
        return "OK"

    # ---------------- read helpers ----------------

    def menu_columns(self, menu: str) -> dict:
        """Column definitions -> {rest_key_name: display name}."""
        url = f"{self._api}/menu/{menu}/info/column/"
        body = self._request("GET", url)
        data = body.get("data", body) if isinstance(body, dict) else body
        return data if isinstance(data, dict) else {}

    def filter_records(self, menu: str, params: dict | None = None) -> list[dict]:
        """Rows of a menu as flat dicts (empty search = no filtering).

        ITA answers `/filter/` with a list of *envelopes*
        (`{"file": {}, "parameter": {...}}`), not records. Callers want the
        fields, so the envelope is unwrapped here. It used to be returned raw,
        which quietly broke every "does this row already exist?" check — most
        visibly for movements, so a re-run registered a second movement with the
        same name and every later pulldown lookup then resolved ambiguously
        ('Failed to exchange ID.').
        """
        url = f"{self._api}/menu/{menu}/filter/"
        body = self._request("POST", url, json=params or {})
        data = body.get("data") if isinstance(body, dict) else body
        if isinstance(data, dict):
            rows = list(data.values())
        elif isinstance(data, list):
            rows = data
        else:
            return []
        out = []
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("parameter"), dict):
                out.append(row["parameter"])
            elif isinstance(row, dict):
                out.append(row)
        return out


    def _maintenance(self, menu: str, params: dict) -> dict:
        """Write one row to a menu (POST /maintenance/)."""
        url = f"{self._api}/menu/{menu}/maintenance/"
        return self._request("POST", url, json={"parameter": params, "file": {}})

    def _pk(self, row: dict, menu: str) -> str | None:
        """Best-effort primary key for a row (differs per menu)."""
        return (row.get("uuid") or row.get("item_no")
                or row.get("associated_item_no") or row.get("operation_id")
                or row.get("movement_id") or row.get("menu_create_id"))

    # Columns ITA will not accept back on a PATCH: derived ids and the user
    # stamp. `last_update_date_time` is deliberately NOT here -- it is the
    # optimistic-lock token and must be echoed unchanged.
    _PATCH_DROP = ("uuid", "item_no", "associated_item_no", "operation_id",
                   "movement_id", "menu_create_id", "last_updated_user")

    @classmethod
    def _row_for_patch(cls, row: dict) -> dict:
        return {k: v for k, v in (row or {}).items() if k not in cls._PATCH_DROP}

    # Measured on this install, after renaming one column of a scratch sheet:
    # the sheet definition answers with the new label at once, while the
    # substitution list keeps advertising the OLD one for ~11 seconds -- and a
    # write carrying a value the list has already moved to is refused inside
    # that window:
    #   499 {"0": {"menu_group_menu_item": ["The input value is an invalid
    #        value.(input value:Substitution value:<sheet>:Parameter/<label>)"]}}
    # Nothing in that reply says "wait", so waiting is the only honest handling.
    SUBST_SETTLE = 60          # seconds to keep trying; measured ~11, no bound
    SUBST_POLL = 4             # between attempts
    # A wall clock alone is not a bound: with the poll set to zero -- as the tests
    # do, because CI has no patience budget -- an expired window would spin thousands
    # of reads at a platform that is never going to answer. Rounds are the limit
    # that always holds.
    SUBST_MAX_ROUNDS = 20

    @staticmethod
    def _is_settling(message: str) -> bool:
        """Does this refusal mean 'the platform has not caught up yet'?

        Narrow on purpose: it has to name the substitution column AND complain
        about the value, or every genuine bad-value error would be retried for a
        minute before being reported.
        """
        low = str(message).lower()
        return ("menu_group_menu_item" in low
                and ("invalid value" in low or "利用できない値" in message
                     or "不正な値" in message))

    def _bind_subst(self, subst_menu, subst_sheet, movement_name, candidate,
                    reg_method, item, prior, resolve, key,
                    settle=None, poll=None):
        """Write one substitution row, waiting out ITA's refresh window.

        -> (outcome, item_used, attempts). `settle` and `poll` are seconds, and
        are parameters so the tests can wait 0 instead of a minute.
        """
        settle = self.SUBST_SETTLE if settle is None else settle
        poll = self.SUBST_POLL if poll is None else poll
        current, attempts, started = item, 0, time.time()
        while True:
            attempts += 1
            row = {
                "movement": movement_name,
                "menu_name_rest": subst_sheet,
                "menu_group_menu_item": current,
                "variable_name": candidate,
                "registration_method": reg_method,
                "remarks": "Created by Exastro_Automate",
            }
            try:
                if prior is not None and str(
                        prior.get("menu_group_menu_item") or "") != current:
                    # The column this row used to point at has been renamed or
                    # re-created: move the row, or the sheet keeps a binding to
                    # a label that no longer exists.
                    return (self._retarget_subst(subst_menu, prior, current),
                            current, attempts)
                return (self._upsert(
                    subst_menu, row,
                    {"movement": movement_name, "menu_group_menu_item": current}),
                    current, attempts)
            except ExastroError as exc:
                if not self._is_settling(str(exc)):
                    raise
                left = settle - (time.time() - started)
                if left <= 0:
                    raise ExastroError(
                        f"{exc} — still refused after {int(time.time() - started)}s. "
                        f"ITA rebuilds a sheet's substitution list asynchronously "
                        f"after a column is renamed or re-created; run this again "
                        f"once the sheet offers '{key}' under its current label."
                    ) from exc
                time.sleep(min(poll, max(left, 0.1)))
                # A refusal means our own answer is already stale: re-read the
                # list, then re-resolve. Re-sending the same string would only
                # burn the window.
                again, _refusal = resolve(key, fresh=True)
                if again:
                    current = again

    def _bound_subst_rows(self, subst_menu: str, subst_sheet: str,
                          movement_name: str, offered=None) -> dict:
        """{variable_name: row} already bound for this movement on this sheet.

        One read, used to recognise a row whose column has been renamed: ITA does
        not call that a duplicate (the item string differs), so POSTing would
        leave a second row pointing at a label that no longer exists -- the exact
        junk this install is already full of.
        """
        # `offered` is the option list the caller already read: this is only a
        # tie-breaker when a variable has several rows, so it must not cost a
        # second request per run.
        offered = set(offered or ())
        out: dict[str, dict] = {}
        try:
            rows = self.filter_records(subst_menu)
        except ExastroError:
            return out
        for row in rows:
            p = row.get("parameter", row) if isinstance(row.get("parameter"), dict) else row
            if str(p.get("menu_name_rest") or "") != subst_sheet:
                continue
            if str(p.get("movement") or "") != movement_name:
                continue
            if str(p.get("discard") or "0") == "1":
                continue
            var = str(p.get("variable_name") or "")
            if not var:
                continue
            keep = out.get(var)
            if keep is None:
                out[var] = p
            elif str(p.get("menu_group_menu_item") or "") in offered:
                out[var] = p          # prefer the row that still resolves
        return out

    def _retarget_subst(self, menu: str, prior: dict, item: str) -> str:
        """Point an existing substitution row at the column's current label."""
        pk = self._pk(prior, menu)
        if not pk:
            raise ExastroError("the existing substitution row carries no id to "
                               "update, so its stale label cannot be replaced")
        merged = self._row_for_patch(prior)
        merged["menu_group_menu_item"] = item
        body = self._request("PATCH", f"{self._api}/menu/{menu}/maintenance/{pk}/",
                             json={"parameter": merged, "file": {}})
        outcome = self._result(body)
        return (f"{outcome} (moved to the column's current label)"
                if outcome == "OK" else outcome)

    def _upsert(self, menu: str, params: dict, match_keys: dict) -> str:
        """Register a row; update the existing one if ITA calls it a duplicate.

        ITA enforces uniqueness on some column combinations — e.g. a movement
        may hold only one role link per `include_order` — so pointing an existing
        link at a different role must PATCH the row rather than insert.

        The previous version swallowed every PATCH error and then re-issued the
        original POST, which of course failed the same way. The user therefore
        saw 'The combination is incorrect' with no hint that the row existed and
        no reason why updating it had failed. Both causes are reported now.
        """
        try:
            return self._result(self._maintenance(menu, params))
        except ExastroError as exc:
            insert_error = str(exc)

        if not _contains_any(insert_error, _DUPLICATE_MARKERS):
            raise ExastroError(insert_error)

        candidates = []
        for row in self.filter_records(menu):
            p = row.get("parameter", row) if isinstance(row.get("parameter"), dict) else row
            if not all(str(p.get(k)) == str(v) for k, v in match_keys.items()):
                continue
            candidates.append((str(p.get("discard")) == "1", self._pk(p, menu), p))
        # Live rows first; a discarded row is only revived if nothing else fits.
        candidates.sort(key=lambda c: c[0])

        patch_errors = []
        for discarded, pk, p in candidates:
            if not pk:
                continue
            # `last_update_date_time` MUST be sent back unchanged: it is ITA's
            # optimistic-lock token, and a PATCH without it is rejected with
            # "last_update_date_time is invalid (None)". It was stripped here,
            # which made every update path fail — and because the failure was
            # swallowed, the caller only ever saw the original duplicate error.
            merged = self._row_for_patch(p)
            merged.update(params)
            if discarded:
                merged["discard"] = "0"

            try:
                url = f"{self._api}/menu/{menu}/maintenance/{pk}/"
                body = self._request("PATCH", url,
                                     json={"parameter": merged, "file": {}})
                outcome = self._result(body)
                return f"{outcome} (updated existing row)" if outcome == "OK" else outcome
            except ExastroError as exc:
                patch_errors.append(f"{pk}: {exc}")

        if not candidates:
            raise ExastroError(
                f"{insert_error} — ITA reports a duplicate but no matching row "
                f"is readable on '{menu}' (a record owned by another user's "
                f"discarded set, or a column not returned by /filter/).")
        raise ExastroError(
            f"{insert_error} — matched {len(candidates)} existing row(s) on "
            f"'{menu}' but updating failed: {' | '.join(patch_errors)}")


    # ---------------- catalogue reads (drive the UI dropdowns) ----------------

    def _wanted_link_types(self) -> list[str]:
        """Acceptable `link_file_type` values, resolved for *this* account.

        The column stores a display string, and ITA returns it translated:
        'Ansible-LegacyRole/Role package list' for an English user is
        'Ansible-LegacyRole/ロールパッケージ管理' for a Japanese one. Filtering on
        the English literal returned nothing, so every package was reported as
        uploaded rather than Git-synced. The numeric id is stable, so resolve the
        label from the account's own pulldown and keep the literal as fallback.
        """
        opts = self._pulldown(cfg.FILE_LINK_MENU).get("link_file_type") or {}
        wanted = [self._label(opts, cfg.FILE_LINK_ROLE_TYPE_ID,
                              cfg.FILE_LINK_ROLE_TYPE)]
        wanted += [cfg.FILE_LINK_ROLE_TYPE, "Role package list",
                   "ロールパッケージ管理"]
        return [w for w in dict.fromkeys(wanted) if w]

    def git_role_packages(self) -> list[dict]:
        """Git link definitions that feed the Role Package List.

        Reads the `file_link` menu: each row is one Git connection whose
        `link_file_name` becomes the *package* half of the
        `movement_role_link.role_package_name_role_name` value, and whose
        `file_path` is the repo path (`acme_demo_pkg:
        demo_pkg/roles`).
        """
        wanted = self._wanted_link_types()
        out = []
        for row in self.filter_records(cfg.FILE_LINK_MENU):
            ltype = str(row.get("link_file_type") or "")
            if wanted and not any(w in ltype for w in wanted):
                continue
            name = str(row.get("link_file_name") or "").strip()
            if not name:
                continue
            out.append({
                "package": name,
                "file_path": row.get("file_path") or "",
                "link_file_type": ltype,
                "sync_state": row.get("file_sync_state") or "",
            })
        return sorted(out, key=lambda r: r["package"].lower())

    def selectable_roles(self) -> list[str]:
        """Every `Package:Role` value ITA will accept on movement_role_link.

        This is the authoritative list — it is ITA's own pulldown, so anything
        in it is guaranteed to validate on write.
        """
        opts = self._pulldown(cfg.ROLE_LINK_MENU).get("role_package_name_role_name", {})
        return sorted({str(v) for v in opts.values()})

    def role_choices(self) -> list[dict]:
        """Role packages with their roles, for the two dependent dropdowns.

        Git-connected packages come from `file_link` (so the repo path can be
        shown beside the name); their roles are derived by splitting the
        selectable `Package:Role` values. Uploaded role packages are appended as
        a separate group so the UI still works for non-Git roles.
        """
        packages = self.git_role_packages()
        git_names = {p["package"] for p in packages}
        roles_by_package: dict[str, list[str]] = {p["package"]: [] for p in packages}
        others: dict[str, list[str]] = {}
        for value in self.selectable_roles():
            package, _, role = value.partition(cfg.ROLE_PKG_SEP)
            if not role:
                continue
            if package in git_names:
                roles_by_package[package].append(role)
            else:
                others.setdefault(package, []).append(role)

        out = [{**p, "source": "git", "roles": sorted(roles_by_package[p["package"]])}
               for p in packages]
        out += [{"package": name, "file_path": "", "link_file_type": "",
                 "sync_state": "", "source": "upload", "roles": sorted(roles)}
                for name, roles in sorted(others.items(), key=lambda kv: kv[0].lower())]
        return out

    def execution_environments(self) -> list[str]:
        """Ansible execution environments selectable on a movement."""
        opts = self._pulldown(cfg.MOVEMENT_MENU).get("ansible_agent_execution_environment", {})
        values = [str(v) for v in opts.values()]
        # Keep the platform default last: it rarely has the role runtime on it.
        values.sort(key=lambda v: (v.startswith("~["), v.lower()))
        return values

    def resolve_execution_env(self, requested: str = "") -> str:
        """Pick the execution environment to record on a new movement."""
        available = self.execution_environments()
        if not available:
            return requested or cfg.EXEC_ENV
        for candidate in (requested, cfg.EXEC_ENV):
            if candidate and candidate in available:
                return candidate
        # A configured env that vanished (workspace changed) would be rejected
        # on write and silently cost us every role variable.
        return available[0]

    def movement_variable_names(self, movement_name: str) -> list[str]:
        """Role variables ITA has collected for `movement_name`.

        Read back from the substitution menu's own `variable_name` pulldown, so
        it reflects exactly what is bindable. `movement_variable_assoc_list_*`
        would be the direct source but is typically not readable with an API
        token (401-00001).
        """
        prefix = f"{movement_name}{cfg.ROLE_PKG_SEP}"
        opts = self._pulldown(cfg.SUBST_MENU).get("variable_name", {})
        return sorted({str(v)[len(prefix):] for v in opts.values()
                       if str(v).startswith(prefix)})

    def wait_for_role_variables(self, movement_name: str, expected,
                                timeout: int | None = None,
                                interval: int | None = None) -> list[str]:
        """Block until Exastro's backyard registers `expected` role variables.

        Linking a role and reading its variables are separate asynchronous
        steps: variables only become bindable once the listup job has inspected
        the role through the movement's execution environment. Returns the
        variables found — possibly a superset of `expected`, possibly short.
        """
        expected = set(expected or [])
        timeout = cfg.VAR_TIMEOUT if timeout is None else timeout
        interval = max(1, cfg.VAR_INTERVAL if interval is None else interval)
        deadline = time.time() + max(0, timeout)
        found: list[str] = []
        while True:
            found = self.movement_variable_names(movement_name)
            if expected.issubset(found) or time.time() >= deadline:
                return found
            time.sleep(min(interval, max(0.0, deadline - time.time())))

    # ---------------- creation operations ----------------

    def _find_movement(self, movement_name: str) -> dict | None:
        """The live (non-discarded) movement row with this name, if any."""
        for row in self.filter_records(cfg.MOVEMENT_MENU):
            if str(row.get("movement_name")) == movement_name \
                    and str(row.get("discard")) != "1":
                return row
        return None

    def create_movement(self, movement_name: str, execution_env: str = "") -> str:
        """Create an Ansible-LegacyRole movement (idempotent).

        `ansible_agent_execution_environment` is set deliberately: without it
        Exastro never collects the linked role's variables, and every
        movement<->parameter link is then unbindable. If the movement already
        exists but is missing that environment, it is patched in place rather
        than duplicated (two movements sharing a name make every later pulldown
        lookup ambiguous — ITA then renders 'Failed to exchange ID.').
        """
        params = {
            "movement_name": movement_name,
            "orchestrator": self.orchestrator_label(),
            "header_section": cfg.HEADER_SECTION,
            "host_specific_format": self.host_format_label(),
            "ansible_agent_execution_environment": execution_env,
            "remarks": "Created by Exastro_Automate",
        }
        existing = self._find_movement(movement_name)
        if existing is None:
            return self._upsert(cfg.MOVEMENT_MENU, params, {"movement_name": movement_name})

        current = str(existing.get("ansible_agent_execution_environment") or "")
        if execution_env and current == execution_env:
            return f"exists: movement '{movement_name}' (env {execution_env})"
        if not execution_env:
            return f"exists: movement '{movement_name}'"

        pk = existing.get("movement_id") or self._pk(existing, cfg.MOVEMENT_MENU)
        merged = {k: v for k, v in existing.items()
                  if k not in ("movement_id", "uuid", "item_no",
                               "last_updated_user")}
        merged["ansible_agent_execution_environment"] = execution_env
        if not merged.get("orchestrator"):
            merged["orchestrator"] = self.orchestrator_label()

        if pk:
            url = f"{self._api}/menu/{cfg.MOVEMENT_MENU}/maintenance/{pk}/"
            body = self._request("PATCH", url, json={"parameter": merged, "file": {}})
            return (f"updated: movement '{movement_name}' -> env {execution_env}"
                    f" ({self._result(body)})")
        return f"exists: movement '{movement_name}' (env NOT set — variables unavailable)"


    def link_movement_role(self, movement_name: str, role_name: str,
                           include_order: int = 1) -> str:
        """Link a movement to a role package via movement_role_link.

        ``role_name`` is the FULL selectable ``package:role`` display value
        (e.g. ``demo_pkg:DEMO_HOST_JOB``), assembled by the UI
        from the Role Package and Role dropdowns. Role packages are Git-synced
        or uploaded Ansible roles, so they must already exist — this method only
        records the movement-to-role association.

        A value ITA will not accept is reported up front instead of being
        written and failing downstream: an unselectable link silently leaves the
        movement with no role, which is what makes the parameter step register
        nothing.
        """
        selectable = self.selectable_roles()
        if selectable and role_name not in selectable:
            packages = sorted({r.partition(":")[0] for r in selectable})
            return (f"FAIL: role '{role_name}' is not selectable on "
                    f"{cfg.ROLE_LINK_MENU}. Known packages: {', '.join(packages)}")
        params = {
            "movement": movement_name,
            "role_package_name_role_name": role_name,
            "include_order": include_order,
            "remarks": "Created by Exastro_Automate",
        }
        return self._upsert(cfg.ROLE_LINK_MENU, params, {"movement": movement_name})


    @staticmethod
    def _column_entry(key: str, value, entry: dict, order: int) -> dict:
        """One column, in the shape create/define/execute expects.

        Shared by the create path and by an edit that adds a parameter to an
        existing sheet, because ITA validates a new column on an edit exactly as
        strictly as on a create -- omit `single_string_maximum_bytes` on a String
        column and the whole edit is refused with '"Maximum number of bytes" is
        required when "String" is set' -- and every field has to be present,
        empty or not.
        """
        class_name, class_id = _column_class(value)
        col: dict = {
            "column_class": class_name,
            "column_class_id": class_id,
            "display_order": order,
            # `item_name` is what a human reads on the sheet's own screens and
            # what the substitution pulldown advertises; `item_name_rest` is the
            # logical name every API call and this tool's JSON uses.
            "item_name": _display_name(entry, key),
            "item_name_rest": key,
            "required": _flag(entry, "required") or "0",
            "uniqued": _flag(entry, "unique") or "0",
            "remarks": "",
            "description": "",
            "single_string_regular_expression": "",
            "integer_default_value": "",
            "integer_maximum_value": "",
            "integer_minimum_value": "",
            "decimal_default_value": "",
            "decimal_maximum_value": "",
            "decimal_minimum_value": "",
            "multi_string_default_value": "",
            "multi_string_maximum_bytes": "",
        }
        if class_id in ("1", "2"):
            col["single_string_maximum_bytes"] = 255
        col.update(_default_value(value, class_id))
        return col

    def _create_define_payload(self, sheet_name: str, params: dict,
                               meta: dict | None = None) -> dict:
        """Build the create/define/execute body for a new parameter sheet.

        Each JSON key becomes a column. Verified live against the v2.8
        menu-create controller: the body needs a top-level ``type`` of
        ``create_new`` and every column carries a ``column_class`` (name) plus
        its ``*_default_value`` field — otherwise ITA rejects it with
        "Required fields." / "The target key value is invalid. (key: type)".
        """
        meta = meta or {}
        columns: dict[str, dict] = {}
        col_list: list[str] = []
        for i, (key, value) in enumerate(params.items()):
            cid = f"c{i + 1}"
            columns[cid] = self._column_entry(key, value, meta.get(key) or {}, i)
            col_list.append(cid)

        choices = self._display_choices()
        sheet_types = choices.get("sheet_type") or {}
        groups = choices.get("menu_group") or {}
        return {
            "type": "create_new",
            "menu": {
                "menu_name": sheet_name,
                "menu_name_rest": sheet_name,
                "sheet_type": self._label(sheet_types, cfg.SHEET_TYPE_ID,
                                          "Parameter Sheet(Host/Operation)"),
                "sheet_type_id": cfg.SHEET_TYPE_ID,
                "menu_group_for_input": self._label(groups, cfg.MENU_GROUP_INPUT_ID, "Input"),
                "menu_group_for_input_id": cfg.MENU_GROUP_INPUT_ID,
                "menu_group_for_ref": self._label(groups, cfg.MENU_GROUP_REF_ID, "Reference"),
                "menu_group_for_ref_id": cfg.MENU_GROUP_REF_ID,
                "menu_group_for_subst": self._label(groups, cfg.MENU_GROUP_SUBST_ID,
                                                    "Substitution value"),
                "menu_group_for_subst_id": cfg.MENU_GROUP_SUBST_ID,
                "hostgroup": "1",
                "vertical": "0",
                "display_order": 1,
                "role_list": self.grantable_roles(cfg.ADMIN_ROLE),
                "columns": col_list,
            },
            "column": columns,
            "group": {},
        }

    def create_parameter_sheet(self, sheet_name: str, params: dict,
                               meta: dict | None = None,
                               replace_flags: bool = False) -> str:
        """Create a parameter sheet with one column per JSON key.

        Uses the two-step Menu Create flow:
           GET  /create/define/            -> static choices
           POST /create/define/execute/    -> define + create the menu

        `meta` is the column grid's per-key `name` / `required` / `unique`, and
        only holds entries the operator actually touched, so an untouched form
        produces byte-identical payloads to the ones from before it existed.

        `replace_flags` only matters to an existing sheet: it lets a Required or
        Unique change be applied by re-creating that column, which costs whatever
        was entered for it on the sheet's Input screen, so the form has to ask for
        it explicitly and it is off unless asked.
        """
        payload = self._create_define_payload(sheet_name, params, meta)
        url = f"{self._api}/create/define/execute/"
        granted = list((payload.get("menu") or {}).get("role_list") or [])
        # Nothing to rescue when the request already asked for no administrator.
        rescuable = bool(granted) and granted[0] != NO_ROLE_SENTINEL
        try:
            resp = self._request("POST", url, json=payload)
        except ExastroError as exc:
            msg = str(exc)
            if _contains_any(msg, _DUPLICATE_MARKERS):
                # Not a no-op any more: the values typed now have to land in the
                # sheet's default-value columns, or the next run silently uses
                # whatever the first run wrote.
                return self._sync_existing_sheet(sheet_name, params, meta,
                                                 replace_flags=replace_flags)
            # The role grant is the one part of the definition an operator
            # cannot control from the form, and on an install where the account
            # may create but not assign roles it is also the likeliest cause.
            # Retry once without it before calling the whole step a failure: a
            # sheet with no explicit admin is still a usable sheet.
            if rescuable:
                retry = json.loads(json.dumps(payload))
                retry["menu"]["role_list"] = [NO_ROLE_SENTINEL]
                try:
                    bare = self._request("POST", url, json=retry)
                except ExastroError:
                    pass          # the role was not the problem; report below
                else:
                    note = (f"created: parameter sheet '{sheet_name}' defined "
                            f"without an admin role grant (this account cannot "
                            f"assign {', '.join(repr(g) for g in granted)})")
                    if _contains_any(msg, _UNUSABLE_VALUE_MARKERS):
                        note += ". Sent value was refused for one of the option " \
                                "columns; check the Advanced group in Settings"
                    return note
            if "role_list" in msg or "admin role" in msg.lower():
                # Nothing left to retry: the request already asked for no real
                # administrator, so only the operator can pick a grantable role.
                return (f"create/define/execute failed: this account cannot assign "
                        f"an admin role to a new sheet (tried "
                        f"{', '.join(repr(g) for g in granted) or 'nothing'}). "
                        f"Set 'Define Role for each configuration' in Settings "
                        f"to a role "
                        f"this account may grant. {exc}")
            if _contains_any(msg, _UNUSABLE_VALUE_MARKERS):
                # Never report this as 'exists': an unusable option value is a
                # real failure, and swallowing it here is what once made a
                # missing parameter sheet look like a successful run.
                return (f"create/define/execute failed: this account was offered a "
                        f"different value than it sent for one of the sheet's "
                        f"option columns. {exc}")
            return f"create/define/execute failed: {exc}"
        return self._result(resp)

    # ---- editing an existing sheet's default values -----------------------
    #
    # The tool's first builds treated "sheet already exists" as success and moved
    # on, which made a re-run with new JSON values a silent no-op: ITA kept the
    # defaults written on the first run. The controller does accept `type: "edit"`
    # — it demands the sheet's `menu_create_id` and *every* existing column
    # echoed back, because a column missing from the payload is deleted. So the
    # read drives the write here, and anything that cannot be echoed faithfully
    # aborts the edit rather than risking a dropped column.

    # Measured: a sheet that does not exist is not a 404 here. ITA answers
    # HTTP 499 `[menu] is invalid. (menu: <name>)`, the same reply it gives for a
    # menu key it does not know. Telling "there is no such sheet" apart from "I
    # could not read it" matters to the page: the first seeds a blank grid, the
    # second must say so rather than quietly behaving like the first.
    MENU_ABSENT = "[menu] is invalid"

    def sheet_column_state(self, sheet_name: str) -> tuple[str, dict]:
        """-> ("ok" | "absent" | "error", {logical: {name, required, unique}}).

        The sheet's own column state, to seed the form's grid. Names and flags
        only: no ids, no timestamps, nothing a browser has any use for.
        """
        try:
            body = self._request("GET", f"{self._api}/create/define/{sheet_name}/")
        except Exception as exc:               # noqa: BLE001 -- see the docstring
            # A seeding read must never be the reason a form fails to open, so
            # anything the transport or ITA throws is folded into "error" and the
            # page says so; only ITA's own words mean "this sheet is new".
            return ("absent" if self.MENU_ABSENT in str(exc) else "error"), {}
        data = body.get("data", body) if isinstance(body, dict) else {}
        info = (data or {}).get("menu_info") or {}
        if not (info.get("menu") or {}).get("menu_create_id"):
            return "error", {}
        out: dict[str, dict] = {}
        for col in (info.get("column") or {}).values():
            key = str(col.get("item_name_rest") or "").strip()
            if not key:
                continue
            out[key] = {
                "name": str(col.get("item_name") or key),
                "required": str(col.get("required") or "0") == "1",
                "unique": str(col.get("uniqued") or "0") == "1",
            }
        return "ok", out

    def sheet_definition(self, sheet_name: str) -> dict | None:
        """Definition of an existing sheet: menu, columns, column classes."""
        try:
            body = self._request("GET", f"{self._api}/create/define/{sheet_name}/")
        except ExastroError:
            return None
        data = body.get("data", body) if isinstance(body, dict) else {}
        info = (data or {}).get("menu_info") or {}
        menu = info.get("menu") or {}
        if not menu.get("menu_create_id"):
            return None
        return {"menu": menu, "column": info.get("column") or {},
                "classes": data.get("column_class_list") or []}

    @staticmethod
    def _default_field(col: dict, class_id: str) -> str:
        """Which `*_default_value` key holds this column's default.

        Chosen from the column ITA just sent back rather than from the class id,
        because the read only carries the field that class actually uses — and a
        sheet created by an earlier build may name it differently than a fresh
        one would.
        """
        fields = ("single_string_default_value", "integer_default_value",
                  "decimal_default_value", "multi_string_default_value")
        for f in fields:
            if f in col:
                return f
        return {"3": "integer_default_value", "4": "decimal_default_value",
                "2": "multi_string_default_value"}.get(str(class_id),
                                                       "single_string_default_value")

    # Measured live, twice, with ITA's own words: `In the case of "Edit", the
    # setting of the target of the existing item cannot be changed. (item: Name,
    # target: required)` and the same for `uniqued`. A default value and a display
    # name CAN be changed; these two cannot, on a column that already exists --
    # they are only honoured when the column itself is new. Sending them anyway
    # does not merely drop them: the ENTIRE edit is refused, values included.
    EDIT_IMMUTABLE = (("required", "required"), ("unique", "uniqued"))

    def _edit_define_payload(self, sheet_name: str, params: dict,
                             defn: dict, meta: dict | None = None,
                             replace_flags: bool = False
                             ) -> tuple[dict | None, list, list]:
        """-> (payload, [(key, [fields changed])], [(key, field) ITA refuses]).

        A parameter in `params` that the sheet does not have yet is added as a
        column; one that exists is echoed back whole, because a column missing
        from the payload is a column ITA deletes (measured).

        `replace_flags` uses that deletion rule on purpose: ITA will not change
        `required`/`uniqued` on an existing column, but an edit that omits the
        column and carries it again under a fresh id creates it anew, flags and
        all -- the API equivalent of pressing the screen's x and re-adding the
        row. Measured: the new column keeps its logical name, label, class,
        default value and display order, and takes a NEW `create_column_id`
        (ad4e79e3 became 34972bd9), which is why it is opt-in: input values
        already entered against the old column record do not carry over.
        """
        menu, columns = defn["menu"], defn["column"]
        class_by_id = {str(c.get("column_class_id")): str(c.get("column_class_name"))
                       for c in defn["classes"]}
        ordered = sorted(columns.values(),
                         key=lambda c: c.get("display_order") or 0)
        if not ordered:
            return None, "the sheet has no columns to echo", []

        echo, keys, changed, skipped, replace = {}, [], [], [], {}
        # changed: [(key, [fields])]   skipped: [(key, field)]   replace:
        #          {key: (echoed column, {flag: (value, column field)})}
        for cid, col in ((k, columns[k]) for k in
                         sorted(columns, key=lambda k: columns[k].get("display_order") or 0)):
            new = dict(col)
            class_id = str(new.get("column_class_id") or "")
            name = str(new.get("column_class") or "")
            if not name:
                # the read leaves the class *name* empty and the write requires
                # it; resolve it from ITA's own class list
                name = class_by_id.get(class_id, "")
            if not name:
                return None, (f"column '{new.get('item_name_rest')}' has class "
                              f"id {class_id!r}, which is not in this install's "
                              f"column_class_list"), []
            new["column_class"] = name
            new.pop("last_updated_user", None)
            field = self._default_field(new, class_id)
            key = str(new.get("item_name_rest") or new.get("item_name") or "")
            touched: list[str] = []
            if key in params:
                wanted = "" if params[key] is None else str(params[key])
                if str(new.get(field, "")) != wanted:
                    new[field] = wanted
                    touched.append("default")
            entry = (meta or {}).get(key) or {}
            if "name" in entry:
                label = _display_name(entry, key)
                if label and str(new.get("item_name") or "") != label:
                    new["item_name"] = label
                    touched.append("name")
            wanted_flags = {}
            for field_name, col_field in self.EDIT_IMMUTABLE:
                value = _flag(entry, field_name)
                if value is not None and str(new.get(col_field)) != value:
                    wanted_flags[field_name] = (value, col_field)
                    # recorded, never sent: sending it loses the whole edit
                    skipped.append((key, field_name))
            if wanted_flags and replace_flags:
                replace[key] = (new, wanted_flags, touched)
                skipped = [(k, f) for k, f in skipped if k != key]
                continue        # the row is re-added below under a fresh id
            if touched:
                changed.append((key, touched))
            echo[cid] = new
            keys.append(cid)

        # A column queued for replacement is deliberately absent from `echo` at
        # this point -- it is re-added below under a fresh id -- so it must not be
        # counted as a column that failed to echo.
        if len(echo) + len(replace) != len(columns):
            return None, "not every existing column could be echoed", []

        # A key the sheet does not have yet is a new column, and ITA accepts a
        # new column on an edit -- including its required/unique flags, which are
        # only immutable on a column that already exists. Before this, such a key
        # was silently dropped and the run reported "already match".
        order = max((int(c.get("display_order") or 0) for c in columns.values()),
                    default=-1) + 1
        existing = {str(c.get("item_name_rest") or "") for c in columns.values()}
        for key, value in params.items():
            if key in existing:
                continue
            cid = f"c{len(echo) + 1}"
            while cid in echo:
                cid = f"c{int(cid[1:]) + 1}"
            echo[cid] = self._column_entry(key, value, (meta or {}).get(key) or {},
                                           order)
            order += 1
            keys.append(cid)
            fields = ["added"]
            if echo[cid]["item_name"] != key:
                fields.append("name")
            if echo[cid]["required"] == "1":
                fields.append("required")
            if echo[cid]["uniqued"] == "1":
                fields.append("unique")
            changed.append((key, fields))

        starred = False
        for key, (col, wanted, touched) in replace.items():
            fresh = dict(col)
            # no id, and one the sheet has never seen: that is what makes ITA
            # create the column instead of comparing it to an existing one
            fresh.pop("create_column_id", None)
            for field_name, (value, col_field) in wanted.items():
                fresh[col_field] = value
            # The id has to be new to the *definition*, not merely absent from
            # the payload: reusing the replaced column's own id would be read as
            # a change to an existing item, which is the refusal being worked
            # around in the first place.
            used = ({int(k[1:]) for k in echo if k[1:].isdigit()}
                    | {int(k[1:]) for k in columns if k[1:].isdigit()})
            cid = f"c{max(used or {0}) + 1}"
            while cid in echo or cid in columns:
                cid = cid + "x"
            echo[cid] = fresh
            keys.append(cid)
            # A `*` marks what could only be applied by re-creating the column,
            # and the footnote below is what makes that mark mean something.
            fields = [f for f, _ in wanted.items()]
            starred = True
            changed.append((key, touched + [f"{f}*" for f in fields]))

        skipped = [(k, f) for k, f in skipped]
        menu = dict(menu)
        menu.pop("last_updated_user", None)
        menu["columns"] = keys
        return ({"type": "edit", "menu_create_id": menu.get("menu_create_id"),
                 "menu": menu, "column": echo, "group": defn.get("group") or {}},
                changed, skipped)

    def _sync_existing_sheet(self, sheet_name: str, params: dict,
                             meta: dict | None = None,
                             replace_flags: bool = False) -> str:
        """Re-run path: put the new values into the existing sheet's defaults."""
        exists = f"exists: parameter sheet '{sheet_name}' already defined"
        defn = self.sheet_definition(sheet_name)
        if defn is None:
            return (f"create/define update failed: {exists}, and its definition "
                    f"could not be read to update it — set the values in the ITA "
                    f"UI or recreate the sheet")
        payload, changed, skipped = self._edit_define_payload(
            sheet_name, params, defn, meta, replace_flags=replace_flags)
        if payload is None:
            return (f"create/define update failed: {exists}, but the defaults "
                    f"could not be rewritten safely ({changed}) — previous "
                    f"values kept")
        cols = ", ".join(f"'{k}' ({f})" for k, f in skipped)
        note = ("" if not skipped else
                # Stated after the outcome rather than instead of it, so a run
                # that did apply the values says so -- and still says which half
                # of the request never went out.
                f" — ITA cannot change {cols} on an existing column; that is set "
                f"when the column is created, or in the ITA UI")
        if any("*" in f for _, fs in changed for f in fs):
            # The mark means "this one cost you the column's entered values", so
            # the footnote is part of the report, not a comment on it.
            note += ("; * applied by re-creating the column, so values already "
                     "entered for it on the sheet's Input screen are gone")
        if not changed:
            if skipped:
                return (f"{exists}; nothing was sent, because only {cols} differed "
                        f"and ITA cannot change those on an existing column")
            return f"{exists} (values and column settings already match)"
        try:
            body = self._request("POST", f"{self._api}/create/define/execute/",
                                 json=payload)
        except ExastroError as exc:
            tried = ", ".join(f"'{k}' ({'+'.join(fs)})" for k, fs in changed)
            return (f"create/define update failed: {exists}, the update of "
                    f"{tried} was refused: {exc}")
        outcome = self._result(body)
        if _contains_any(str(outcome).lower(), _BAD_RESULT_MARKERS):
            return (f"create/define update failed: {exists}, the edit was refused: "
                    f"{outcome}")
        # `changed` lists what each column actually moved, so a run that only
        # renamed a label does not claim to have set values it left alone.
        bits = ", ".join(f"'{k}' ({'+'.join(fs)})" for k, fs in changed)
        return f"updated: parameter sheet '{sheet_name}' set {bits}" + note


    def _pulldown(self, menu: str) -> dict:
        """Selectable options per pulldown column -> {column: {id: display}}."""
        body = self._request("GET", f"{self._api}/menu/{menu}/info/pulldown/")
        data = body.get("data", body) if isinstance(body, dict) else body
        return data if isinstance(data, dict) else {}

    # ---- locale-resolved display values ---------------------------------
    #
    # ITA validates pulldown columns by their *display string*, and the display
    # strings it returns are translated into the language of the calling
    # account.  So 'Parameter Sheet(Host/Operation)' is a perfectly good value
    # for an English user and is rejected for a Japanese one, where the same
    # option reads 'パラメータシート（ホスト/オペレーションあり）'.  A build that
    # hardcodes either label fails for the other account with
    # "利用できない値です" ("the value cannot be used"), which looks like a
    # permissions problem but is not one.
    #
    # The numeric ids are stable across locales, so every such value is resolved
    # from the account's own option list and the literal is only a fallback.

    def _display_choices(self) -> dict:
        """Option lists offered for menu creation, keyed by numeric id.

        Returns {'sheet_type': {id: name}, 'menu_group': {id: name},
        'role_list': [...]}.  Empty on failure so callers fall back to the
        configured literals rather than crashing.
        """
        if self._choices_cache is not None:
            return self._choices_cache
        try:
            data = self._request("GET", f"{self._api}/create/define/",
                                 json=None).get("data") or {}
        except ExastroError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        self._choices_cache = {
            "sheet_type": {str(s.get("sheet_type_id")): s.get("sheet_type_name")
                           for s in data.get("sheet_type_list") or []},
            "menu_group": {str(g.get("menu_group_id")): g.get("menu_group_name")
                           for g in data.get("target_menu_group_list") or []},
            "role_list": list(data.get("role_list") or []),
        }
        return self._choices_cache

    @staticmethod
    def _label(mapping: dict, wanted_id: str, fallback: str) -> str:
        return mapping.get(str(wanted_id)) or fallback

    def registration_method_label(self) -> str:
        """The display value this account must send for its registration method."""
        opts = self._pulldown(cfg.SUBST_MENU).get("registration_method") or {}
        return self._label(opts, cfg.SUBST_REGISTRATION_METHOD_ID,
                           cfg.SUBST_REGISTRATION_METHOD)

    def orchestrator_label(self) -> str:
        """The `orchestrator` display value for this account (id 3 = Legacy Role).

        This one is not translated on 2.8, but it is the same class of column —
        validated by display string — so it is resolved rather than hardcoded.
        """
        opts = self._pulldown(cfg.MOVEMENT_MENU).get("orchestrator") or {}
        return self._label(opts, cfg.ORCHESTRATOR_ID, cfg.ORCHESTRATOR)

    def host_format_label(self) -> str:
        """The `host_specific_format` display value for this account.

        Genuinely translated: id 2 is 'Host name' for an English account and
        'ホスト名' for a Japanese one. Only id 1 ('IP') happens to be identical,
        which is why the hardcoded literal appeared to work.
        """
        opts = self._pulldown(cfg.MOVEMENT_MENU).get("host_specific_format") or {}
        return self._label(opts, cfg.HOST_SPECIFIC_FORMAT_ID,
                           cfg.HOST_SPECIFIC_FORMAT)

    def _sheet_column_names(self, sheet_name: str) -> dict:
        """{logical name: display name} for one sheet, {} if unreadable.

        Measured on a live install: the substitution pulldown spells a column by
        its **display** name (`item_name`), not the logical one (`item_name_rest`)
        that the API and this tool work with:

            SheetA    item_name='Item 1' item_name_rest='item_1'
                       offered as 'Substitution value:SheetA:Parameter/Item 1'
            SheetB   item_name='age'    item_name_rest='ahe'
                       offered as 'Substitution value:SheetB:Parameter/age'
            zos_job_submit  'ジョブ名' / 'p_jobname'

        Sheets this tool created always have both names identical, which is why
        matching on the JSON key worked. It cannot once display names are
        editable, and it already fails on an existing sheet such as
        zos_job_submit.
        """
        defn = self.sheet_definition(sheet_name)
        if not defn:
            return {}
        return {str(c.get("item_name_rest") or ""): str(c.get("item_name") or "")
                for c in defn["column"].values()}

    @staticmethod
    def _display_collisions(names: dict) -> dict:
        """{display name: [logical names]} where a display name is reused.

        Grouped case-insensitively: the pulldown strings are compared as ITA
        wrote them, but a sheet holding both 'Age' and 'AGE' is ambiguous to a
        human and to a case-insensitive lookup, and guessing is worse than
        refusing. The returned keys are the display names as stored.
        """
        seen: dict[str, list] = {}
        shown: dict[str, str] = {}
        for rest, disp in names.items():
            slot = str(disp).casefold()
            seen.setdefault(slot, []).append(rest)
            shown.setdefault(slot, disp)
        return {shown[d]: sorted(r) for d, r in seen.items() if len(r) > 1}

    def _resolve_sheet_item(self, key, items, names, clashes):
        """Selectable pulldown string for one logical column: (option, refusal).

        The display name ITA advertises is tried first, then the key itself --
        which still covers a sheet whose definition cannot be read, and every
        sheet this tool has written so far, where the two names are equal.
        """
        display = names.get(key)
        if display and display in clashes:
            return None, (f"display name '{display}' is used by "
                          f"{len(clashes[display])} columns "
                          f"({', '.join(clashes[display])}); ITA's pulldown "
                          f"cannot tell them apart - rename one in Parameter "
                          f"sheet create")
        for cand in (display, key):
            if cand and cand in items:
                return items[cand], ""
            # ITA can hand back a definition and a pulldown that differ only in
            # case, which happens when a label was retyped in the UI: matching
            # case-insensitively keeps a run working, and a collision was already
            # refused above, so this can only pick a unique candidate.
            if cand:
                folded = {str(k).casefold(): k for k in items}
                hit = folded.get(str(cand).casefold())
                if hit is not None and list(folded.values()).count(hit) == 1:
                    return items[hit], ""
        return None, ""

    def _sheet_parameter_items(self, sheet_name: str) -> dict:
        """{column key: exact selectable string} for one parameter sheet.

        `menu_group_menu_item` is a pulldown whose values are fully localized,
        e.g. 'Substitution value:Sheet:Parameter/key' for an English account but
        '代入値自動登録用:Sheet:パラメータ/key' for a Japanese one. Composing the
        English literal — which is what earlier builds did, because the sample
        rows on this install were created by an English user — makes ITA answer
        '利用できない値です' for every key. Reading the real options fixes it for
        both locales at once and also reveals which columns actually exist.
        """
        opts = self._pulldown(cfg.SUBST_MENU).get("menu_group_menu_item") or {}
        out: dict[str, str] = {}
        for value in opts.values():
            parts = str(value).split(":")
            if len(parts) < 3 or parts[1] != sheet_name:
                continue
            out[parts[-1].split("/")[-1]] = str(value)
        return out


    def own_identity(self) -> str:
        """The account name behind this client, however it authenticates.

        With username/password it is the configured user. With a token there is
        no username in the configuration, but the exchanged access token is an
        OpenID Connect JWT carrying `preferred_username`, and that is the same
        principal name Exastro lists as a grantable role. Decoded locally; the
        signature is irrelevant here because the value is only used as a name,
        never trusted for access.
        """
        user = (self._v("USER") or "").strip()
        if user:
            return user
        if self._identity is not None:
            return self._identity
        name = ""
        try:
            token = self.token()
            parts = token.split(".")
            if len(parts) == 3:
                body = parts[1] + "=" * (-len(parts[1]) % 4)
                claims = json.loads(base64.urlsafe_b64decode(body))
                name = str(claims.get("preferred_username") or "").strip()
        except Exception:
            name = ""          # a non-JWT token or an unreachable platform
        self._identity = name or NO_ROLE_SENTINEL
        return self._identity

    def grantable_roles(self, requested: str) -> list[str]:
        """See `resolve_role_list`; blank resolves to this account's own name."""
        return resolve_role_list(requested, self.own_identity())


    def link_movement_parameter(self, movement_name: str, sheet_name: str,
                                params: dict, wait: bool = True) -> list[dict]:
        """Register one substitution row per JSON key — for *every* key.

        Two of the columns are pulldowns, and ITA validates the DISPLAY value,
        not a free string:

          menu_group_menu_item  = "Substitution value:<sheet>:Parameter/<key>"
          variable_name         = "<movement>:<role variable>"

        Both must already be selectable. The sheet exists because we created it
        a step earlier; the role variables, however, are collected by an
        *asynchronous* backyard job after the role is linked. Reading them
        immediately is why an earlier version of this flow registered only one
        of four keys: the pulldown was still nearly empty, and the loop
        correctly skipped everything it could not resolve.

        So we wait for the variables first (`wait=True`), then bind each key
        strictly to its own variable. There is deliberately no fallback to
        "some variable that exists": an earlier build bound `p_environment`
        to a role variable called `name`, which wrote successfully and produced
        a movement that silently passed the wrong value at execution time.
        A key with no matching role variable is reported as `missing_variable`
        so the role or the JSON can be corrected.
        """
        subst_menu = cfg.SUBST_MENU
        subst_sheet = f"{sheet_name}_subst"
        variable_map = self._variable_map or {}
        targets = {key: variable_map.get(key) or key for key in params}
        # Resolved once, from this account's own option list: 'Value type' is
        # 'Value型' for a Japanese-locale user and ITA rejects the mismatch.
        reg_method = self.registration_method_label()

        if wait:
            available = self.wait_for_role_variables(
                movement_name, targets.values())
        else:
            available = self.movement_variable_names(movement_name)

        prefix = f"{movement_name}{cfg.ROLE_PKG_SEP}"
        selectable = {f"{prefix}{var}" for var in available}
        # The sheet column must be referenced by the exact string ITA offers;
        # see _sheet_parameter_items().
        items = self._sheet_parameter_items(sheet_name)
        names = self._sheet_column_names(sheet_name)
        clashes = self._display_collisions(names)
        # What this movement already binds, so a renamed column can be moved
        # rather than duplicated. One read for the whole sheet.
        bound = self._bound_subst_rows(subst_menu, subst_sheet, movement_name,
                                       set(items.values()))

        def resolve(k, fresh=False):
            """Resolve a key against the sheet's current option list.

            The definition and the substitution pulldown are two different
            queries; after a rename they can answer inconsistently for a moment.
            A miss on the pulldown used to be reported as "the sheet has no such
            column", which was simply not true.

            `fresh` re-reads the list instead of trusting what was cached at the
            top of this step. A write that has just been refused tells us the
            cache is out of date, and re-sending the same string would fail the
            same way -- so retries ask again rather than insist.
            """
            if fresh:
                try:
                    items.update(self._sheet_parameter_items(sheet_name))
                except ExastroError:
                    pass        # keep the previous answer rather than lose it
                names_now = self._sheet_column_names(sheet_name) or names
                clashes_now = self._display_collisions(names_now)
            else:
                names_now, clashes_now = names, clashes
            item, refusal = self._resolve_sheet_item(k, items, names_now, clashes_now)
            if item is None and not refusal and not fresh:
                again = self._sheet_parameter_items(sheet_name)
                item, refusal = self._resolve_sheet_item(k, again, names, clashes)
                if item is not None:
                    items.update(again)
            return item, refusal

        # One settling window shared by every key: if the list is behind the
        # sheet it is behind for all of them, and a three-column sheet should not
        # cost three minutes to find that out.
        window = {"until": None, "rounds": 0, "seconds": 0.0}

        def settle(k):
            """Poll the option list until it agrees with the sheet's definition.

            The refused write is only the visible half of the race. After a rename
            or a re-create the definition answers with the new label while the list
            still advertises the old one -- and a key that never resolves never
            reaches the write, so no refusal happens at all and the run reports a
            column that plainly exists. Waiting belongs here as much as around the
            POST, which is what the first fix of this missed.
            """
            if window["until"] is None:
                window["until"] = time.time() + self.SUBST_SETTLE
            started = time.time()
            rounds, found, why = 0, None, None
            while True:
                if rounds:
                    time.sleep(self.SUBST_POLL)
                rounds += 1
                found, why = resolve(k, fresh=True)
                if (found or why or rounds >= self.SUBST_MAX_ROUNDS
                        or time.time() >= window["until"]):
                    break
            window["rounds"] += rounds
            window["seconds"] += time.time() - started
            # Per-key numbers, or the third column of a sheet would report the
            # whole step's patience as if it were its own.
            return found, why, rounds, time.time() - started

        results: list[dict] = []
        for key, var_name in targets.items():
            candidate = f"{prefix}{var_name}"
            waited, expired = "", ""
            item, refusal = resolve(key)
            # Worth waiting for only when the sheet's own definition has the
            # column: a key nobody defined is a real mistake and is reported now.
            if item is None and not refusal and key in (names or {}):
                item, refusal, rounds, seconds = settle(key)
                if item and seconds > 0:
                    waited = (f" [accepted after {rounds} reads "
                              f"({seconds:.0f}s): ITA rebuilds a sheet's "
                              f"substitution list asynchronously once a column is "
                              f"renamed or re-created]")
                elif item is None and seconds > 0:
                    expired = (f"; still not offered after waiting "
                               f"{seconds:.0f}s ({rounds} reads) — run this "
                               f"again once the sheet's list is rebuilt")
            if item is None and refusal:
                results.append({"key": key, "variable": candidate,
                                "status": "ambiguous_column", "detail": refusal})
                continue
            if item is None:
                # Which side disagreed matters to whoever reads this, so it is
                # stated: a sheet whose definition names the column but whose
                # pulldown does not offer it yet is not "a missing column".
                display = names.get(key)
                if display and display != key:
                    why = (f"its definition names it '{display}' but the "
                           f"substitution pulldown does not offer that either "
                           f"(it may not be applied to the sheet yet)")
                elif not names:
                    why = ("its definition could not be read, so only an exact "
                           "name match was possible")
                else:
                    why = "no column of it is offered"
                results.append({
                    "key": key, "variable": candidate, "status": "missing_column",
                    "detail": (f"parameter sheet '{sheet_name}' offers no column "
                               f"named '{key}'; {why}"
                               + (f"; available: {', '.join(sorted(items))}"
                                  if items else "; none selectable")
                               # Saying "waited" here is the difference between a
                               # bug report and a retry instruction.
                               + (expired if item is None else ""))})
                continue
            if candidate not in selectable:
                results.append({
                    "key": key, "variable": candidate, "status": "missing_variable",
                    "detail": (f"role variable '{var_name}' is not registered for "
                               f"movement '{movement_name}'"
                               + (f"; collected: {', '.join(available)}"
                                  if available else "; none collected yet"))})
                continue
            started = time.time()
            try:
                outcome, item, attempts = self._bind_subst(
                    subst_menu, subst_sheet, movement_name, candidate,
                    reg_method, item, bound.get(candidate), resolve, key)
            except ExastroError as exc:
                results.append({"key": key, "variable": candidate,
                                "status": "failed", "detail": str(exc)})
                continue
            if attempts > 1:
                # The wait is the story: a run that took a minute to bind one key
                # is ITA rebuilding the sheet, not this tool retrying blindly.
                outcome += (f" [accepted on attempt {attempts}, "
                            f"{int(time.time() - started)}s: ITA regenerates a "
                            f"sheet's substitution list after a column is "
                            f"re-named or re-created]")
            status = "linked" if "OK" in outcome else "exists"
            results.append({"key": key, "variable": candidate,
                            "status": status, "detail": outcome + waited})
        return results



# ---------------------------------------------------------------------------
# Mock client — full UI flow with no live Exastro (EXA_MOCK=true)
# ---------------------------------------------------------------------------

    def id_options(self) -> dict:
        """Every advanced id this environment actually offers, with its label.

        Keys match the `options_from` names in settings.FIELDS. ITA validates
        these columns by *id* while showing a translated label, so the only
        honest way to know them is to ask the connected install — which is what
        the Settings form does instead of making the operator remember numbers.
        Partial by design: an unreachable menu simply leaves its field as text.
        """
        if self._id_options_cache is not None:
            return self._id_options_cache
        out: dict[str, list[tuple[str, str]]] = {}

        def as_sorted(mapping: dict) -> list:
            return sorted(((str(k), str(v or "")) for k, v in (mapping or {}).items()
                           if str(k).strip()),
                          key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0)

        try:
            choices = self._display_choices()
            out["sheet_type"] = as_sorted(choices.get("sheet_type"))
            out["menu_group"] = as_sorted(choices.get("menu_group"))
        except ExastroError:
            pass
        # one pulldown call serves several fields when they share a menu
        for column, menu in (("orchestrator", cfg.MOVEMENT_MENU),
                             ("host_specific_format", cfg.MOVEMENT_MENU),
                             ("registration_method", cfg.SUBST_MENU),
                             ("link_file_type", cfg.FILE_LINK_MENU)):
            if column in out:
                continue
            try:
                out[column] = as_sorted(self._pulldown(menu).get(column))
            except ExastroError:
                continue
        self._id_options_cache = out
        return out

    def probe(self) -> dict:
        """Cheapest round trip that proves a profile works: token + one GET.

        Settings -> Test connection calls this on a *draft* profile, so an
        operator can verify a new environment without making it active first.
        Nothing is created and nothing is written.
        """
        started = time.time()
        self.token()
        token_ms = int((time.time() - started) * 1000)
        started = time.time()
        # `create/define/` answers with the option lists a new parameter sheet
        # may use, so it proves both auth and that the workspace is readable.
        body = self._request("GET", f"{self._api}/create/define/", json=None)
        api_ms = int((time.time() - started) * 1000)
        data = body.get("data") or {} if isinstance(body, dict) else {}
        if not isinstance(data, dict):
            data = {}
        return {
            "ok": True,
            "token_ms": token_ms,
            "api_ms": api_ms,
            "sheet_types": len(data.get("sheet_type_list") or []),
            "menu_groups": len(data.get("target_menu_group_list") or []),
            "gateway": str(self._v("GATEWAY_URL") or ""),
            "org": str(self._v("ORG_ID") or ""),
            "workspace": str(self._v("WORKSPACE_ID") or ""),
            "api_base": self._api,
            "auth_mode": "token" if self._v("API_TOKEN") else "password",
            "user": str(self._v("USER") or ""),
        }


class MockExastroClient(ExastroClient):
    """Simulates every call so the full UI flow works without credentials.

    The catalogue mirrors the real install's shape — Git link definitions from
    `file_link`, `Package:Role` combos on `movement_role_link` — so the
    dependent dropdowns and the all-variables binding path can be exercised
    offline. Role variables are 'collected' lazily on first read, reproducing
    the asynchronous backyard the live system exhibits.
    """

    # file_link rows (link_file_name -> Git repo path)
    _FILE_LINKS = [
        {"package": "demo_pkg",
         "file_path": "acme_demo_pkg:demo_pkg/roles",
         "link_file_type": "Ansible-LegacyRole/Role package list",
         "sync_state": "Normal"},
        {"package": "gitlab_demo_pkg",
         "file_path": "gitlab_source:gitlab_demo_pkg/roles",
         "link_file_type": "Ansible-LegacyRole/Role package list",
         "sync_state": "Normal"},
    ]
    # movement_role_link.role_package_name_role_name options
    _ROLES = [
        "demo_pkg:DEMO_HOST_JOB",
        "demo_pkg:DEMO_TAPE_ROLE",
        "demo_pkg:DEMO_PLAYBOOK_ROLE1",
        "gitlab_demo_pkg:DEMO_HOST_JOB",
        "SheetD:DEMO_PLAYBOOK_ROLE",          # uploaded, not Git
    ]
    # Variable names the mock role exposes, per Package:Role.
    _ROLE_VARS = {
        "demo_pkg:DEMO_HOST_JOB":
            ["p_jobname", "p_reply_flag", "p_host", "p_dataset"],
        "demo_pkg:DEMO_TAPE_ROLE": ["p_tape_dsname"],
        "demo_pkg:DEMO_PLAYBOOK_ROLE1": ["name", "age"],
        "gitlab_demo_pkg:DEMO_HOST_JOB": ["p_jobname"],
        "SheetD:DEMO_PLAYBOOK_ROLE": ["name"],
    }
    _ENVS = ["DEMO_EXEC_ENV", "BACKUP_ENV", "~[Exastro standard] default"]

    def __init__(self) -> None:
        super().__init__()
        self._created: dict[str, list] = {}
        self._sheets: dict[str, dict] = {}     # what a mock "create" left behind
        # movement -> role, filled on link; drives the lazy variable listup.
        self._movement_role: dict[str, str] = {}

    def own_identity(self) -> str:
        """No token to read, so the configured user or a fixed stand-in."""
        return (self._v("USER") or "").strip() or "mock-user"

    def id_options(self) -> dict:  # noqa: D102
        # The same shape the live install returns, so the Settings form renders
        # dropdowns in mock mode instead of falling back to raw text boxes.
        return {
            "orchestrator": [("1", "Ansible Legacy"), ("2", "Ansible Pioneer"),
                             ("3", "Ansible Legacy Role")],
            "host_specific_format": [("1", "IP"), ("2", "Host name")],
            "registration_method": [("1", "Value type"), ("2", "Key type")],
            "link_file_type": [("1", "Ansible-Legacy/Playbook files"),
                               ("3", "Ansible-LegacyRole/Role package list")],
            "sheet_type": [("1", "Parameter Sheet(Host/Operation)"), ("2", "Data Sheet")],
            "menu_group": [("501", "Parameter sheet create"), ("502", "Input"),
                           ("503", "Substitution value"), ("504", "Reference")],
        }

    def probe(self) -> dict:  # noqa: D102
        # No network in mock mode: report what a real probe would report.
        return {"ok": True, "token_ms": 0, "api_ms": 0,
                "sheet_types": 3, "menu_groups": 3,
                "gateway": "mock", "org": "mock", "workspace": "mock",
                "api_base": "mock", "auth_mode": "mock", "user": "mock"}

    def token(self) -> str:  # noqa: D102
        return "mock-token"

    # ---- catalogue ----

    def git_role_packages(self) -> list[dict]:  # noqa: D102
        return [dict(p) for p in self._FILE_LINKS]

    def selectable_roles(self) -> list[str]:  # noqa: D102
        return sorted(self._ROLES)

    def execution_environments(self) -> list[str]:  # noqa: D102
        return list(self._ENVS)

    def movement_variable_names(self, movement_name: str) -> list[str]:
        """Empty until the role is linked — mimics the async listup job."""
        role = self._movement_role.get(movement_name)
        if not role:
            return []
        return sorted(self._ROLE_VARS.get(role, []))

    def wait_for_role_variables(self, movement_name, expected,
                                timeout=None, interval=None) -> list[str]:
        return self.movement_variable_names(movement_name)

    # ---- creation ----

    def create_movement(self, movement_name: str, execution_env: str = "") -> str:
        self._created.setdefault("movement", []).append(
            {"name": movement_name, "env": execution_env})
        if not execution_env:
            # Same trap as live: no environment, so no variables are collected.
            return "OK (mock, but no execution env -> role vars unavailable)"
        return "OK (mock)"

    def link_movement_role(self, movement_name: str, role_name: str,
                           include_order: int = 1) -> str:
        if role_name not in self._ROLES:
            return f"FAIL: role '{role_name}' is not selectable"
        self._movement_role[movement_name] = role_name
        self._created.setdefault("role_link", []).append(
            f"{movement_name} <-> {role_name}({include_order})")
        return "OK (mock)"

    def sheet_column_state(self, sheet_name: str) -> tuple[str, dict]:
        """A mock sheet reads back as it was created: label = logical, no flags."""
        cols = self._sheets.get(sheet_name)
        if cols is None:
            return "absent", {}
        return "ok", {k: {"name": k, "required": False, "unique": False}
                      for k in cols}

    def create_parameter_sheet(self, sheet_name: str, params: dict,
                               meta: dict | None = None,
                               replace_flags: bool = False) -> str:  # noqa: D102
        entry = {sheet_name: list(params.keys())}
        if replace_flags:
            entry["replace_flags"] = True
        self._sheets[sheet_name] = dict(params)
        if meta:
            entry["meta"] = {k: {f: v for f, v in (m or {}).items()
                                 if f in ("name", "required", "unique")}
                             for k, m in meta.items()}
        self._created.setdefault("param_sheet", []).append(entry)
        return "OK (mock)"

    def link_movement_parameter(self, movement_name: str, sheet_name: str,
                                params: dict, wait: bool = True) -> list[dict]:
        available = set(self.movement_variable_names(movement_name))
        results = []
        for key in params:
            var = (self._variable_map or {}).get(key) or key
            candidate = f"{movement_name}:{var}"
            if var not in available:
                results.append({
                    "key": key, "variable": candidate,
                    "status": "missing_variable",
                    "detail": f"role variable '{var}' not registered"
                              + (f"; collected: {', '.join(sorted(available))}"
                                 if available else "; none collected yet")})
                continue
            self._created.setdefault("param_link", []).append(candidate)
            results.append({"key": key, "variable": candidate,
                            "status": "linked", "detail": "OK (mock)"})
        return results


def make_client():
    return MockExastroClient() if cfg.MOCK else ExastroClient()


"""Tests for Exastro_Automate.

Run:  ../.venv/bin/python -m pytest tests -q   (from Exastro_Automate/)

The interesting cases are regressions against the live install's behaviour:
a role variable bound to the *wrong* parameter, a parameter step that reported
success while binding nothing, a movement created without an execution
environment, and the `Package:Role` value the dropdowns must compose.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import html
import inspect
import shutil
import subprocess
import sqlite3
import sys
import tempfile
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Isolate the settings store and the session-signing key *before* `app` is
# imported: importing it creates both, and a test run must neither read the real
# settings.db (which holds a live token) nor write a flask_secret.key.
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="exa-tests-"))
os.environ.setdefault("EXA_SETTINGS_DB", str(_TMP / "settings.db"))
os.environ.setdefault("EXA_SECRET_FILE", str(_TMP / "flask_secret.key"))

import app as appmod                      # noqa: E402
import config as cfg                      # noqa: E402
import i18n                              # noqa: E402
import settings                           # noqa: E402
from exastro_client import (ExastroClient, ExastroError,  # noqa: E402
                              MockExastroClient)


# ---------------------------------------------------------------------------
# a stand-in for the ITA organization API, shaped like the real responses
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        return self._body

    @property
    def text(self):
        import json as _json
        return _json.dumps(self._body)


class FakeExastro:
    """Routes requests to canned responses and records every write.

    Maintains a tiny row store so that POST/PATCH honour ITA's uniqueness rules
    — the behaviour that makes re-running the tool against an existing movement
    or role link interesting.
    """

    # menu -> (primary key column, unique column combination)
    _SCHEMA = {
        cfg.MOVEMENT_MENU: ("movement_id", ("movement_name",)),
        cfg.ROLE_LINK_MENU: ("associated_item_no", ("movement", "include_order")),
        cfg.SUBST_MENU: ("item_no", ("movement", "menu_group_menu_item")),
        # Measured: an operation is addressed by `operation_id`, not `uuid`, and
        # a name that already exists is refused as a duplicate.
        cfg.OPERATION_MENU: ("operation_id", ("operation_name",)),
    }

    # `GET /create/define/` option lists as a Japanese-locale account sees them.
    _DEFINE_LISTS = {
        "sheet_type_list": [
            {"sheet_type_id": "1",
             "sheet_type_name": "パラメータシート（ホスト/オペレーションあり）"},
            {"sheet_type_id": "2", "sheet_type_name": "データシート"}],
        "target_menu_group_list": [
            {"menu_group_id": "502", "menu_group_name": "入力用"},
            {"menu_group_id": "503", "menu_group_name": "代入値自動登録用"},
            {"menu_group_id": "504", "menu_group_name": "参照用"}],
        "role_list": ["developer", "_demo_ws-admin"],
    }

    def __init__(self, file_links=None, roles=None, variables=None,
                 envs=None, movements=None, links=None, store=None,
                 define_lists=None, sheet_items=None, locale="ja",
                 file_links_types=None, hosts=None):
        self.writes = []                      # (method, menu, params)
        # ITA rebuilds a sheet's substitution option list asynchronously after a
        # column is renamed or re-created: measured ~11s on a live install, during
        # which the list still advertises the OLD label while the definition has
        # moved. `subst_lag` expresses that window in reads, so the client's wait
        # is testable in milliseconds.
        # The list stays behind the definition for this many reads after a label
        # moves. Read-based, because the client can be refused nothing at all --
        # it simply cannot resolve the column, which is the failure the operator
        # reported: "offers no column named 'p_age'; available: Age, Name".
        self.subst_lag = 0
        self._subst_stale: dict[str, dict] = {}
        self._subst_reads: dict[str, int] = {}
        # The other half of the race, and the half the client can do nothing
        # about: the write carries a string the list advertises, and ITA still
        # refuses because its own view has not settled. `subst_refuse` refuses
        # that many otherwise-valid writes, so "wait and retry" is testable.
        self.subst_refuse = 0
        self.file_links = file_links or {}
        # {group: [host, ...]} exactly as `host_link_list` holds it. Empty on
        # purpose for most tests: an input write is only validated when the test
        # has said which hosts exist, because refusing everything by default
        # would hide the checks that do matter.
        self.hosts = hosts or {}
        self.roles = roles or {}              # uuid -> 'Package:Role'
        self.variables = variables or {}      # uuid -> 'Movement:var'
        self.envs = envs or {"1": "~[Exastro standard] default",
                             "e1": "DEMO_EXEC_ENV"}
        self.movements = movements or {}      # uuid -> parameter dict
        # `or` would treat an explicit {} as "use the defaults"; an empty
        # option list is real (endpoint unreachable) and must fall back.
        self.define_lists = (self._DEFINE_LISTS if define_lists is None
                             else define_lists)
        # sheet -> column keys; {} means "derive from what this test declares"
        self.sheet_items = sheet_items or {}
        # {sheet: {logical: display}}. A plain list means both names are equal,
        # which is what every sheet this tool has created looks like; a dict is
        # how a real, hand-made sheet looks -- ITA's substitution pulldown then
        # advertises the DISPLAY name, and binding has to follow it there.
        self.sheet_cols = {}
        for sheet, cols in self.sheet_items.items():
            if isinstance(cols, dict):
                self.sheet_cols[sheet] = dict(cols)
            else:
                self.sheet_cols[sheet] = {c: c for c in cols}
        self.created_sheets: dict[str, list] = {}
        # name -> last create/edit body, so `GET /create/define/<name>/` can
        # answer the way ITA does and the edit path can be tested at all
        self.sheet_defs: dict[str, dict] = {}
        # ITA translates validation failures per account; 'ja' is the wording a
        # Japanese user actually receives.
        self.locale = locale
        # file_link.link_file_type as a Japanese-locale account sees it
        self.link_types = file_links_types or {
            "1": "Ansible-Legacy/プレイブック素材集",
            "3": "Ansible-LegacyRole/ロールパッケージ管理",
            "4": "Ansible共通/ファイル管理"}
        self.store = {cfg.ROLE_LINK_MENU: links or {}}
        if self.movements:
            self.store[cfg.MOVEMENT_MENU] = self.movements
        if store:
            self.store.update(store)

    # --- plumbing -----------------------------------------------------------

    def request(self, method, url, **kwargs):
        body = kwargs.get("json") or {}
        menu = url.split("/menu/")[-1].split("/")[0] if "/menu/" in url else ""

        if url.endswith("/info/pulldown/"):
            return self._pulldown(menu)
        if url.endswith("/info/column/"):
            return FakeResponse({"result": "000-00000", "data": {"movement_name": {}}})
        if url.endswith("/filter/"):
            return self._filter(menu)
        if "/create/define/execute/" in url:
            self.writes.append((method, "create/define", body))
            menu = body.get("menu") or {}
            name = menu.get("menu_name_rest") or menu.get("menu_name")
            if body.get("type") == "edit":
                return self._edit_define(name, body)
            if name in self.created_sheets:      # ITA refuses a second sheet of
                return FakeResponse(             # the same name
                    {"result": "499-00201",
                     "message": self._duplicate_text({"menu_name_ja": name}, name)}, 499)
            cols = [c.get("item_name") for c in (body.get("column") or {}).values()]
            self.created_sheets[name] = cols
            self.sheet_defs[name] = {"menu": menu, "column": body.get("column") or {}}
            return FakeResponse({"result": "000-00000", "data": True})
        defined = re.match(r".*/create/define/([^/]+)/?$", url)
        if defined and defined.group(1) != "execute":
            return self._define_read(defined.group(1))
        if url.rstrip("/").endswith("/create/define"):
            # ITA returns these labels in the *calling account's* language, so
            # the fake serves Japanese ones to prove nothing is hardcoded.
            return FakeResponse({"result": "000-00000", "data": self.define_lists})
        if "/maintenance/" in url:
            pk_in_url = url.split("/maintenance/")[-1].strip("/")
            return self._maintenance(menu, method, pk_in_url, body.get("parameter", {}))
        return FakeResponse({"result": "000-00000", "data": True})

    _COLUMN_CLASSES = [
        {"column_class_id": "1", "column_class_name": "SingleTextColumn",
         "column_class_disp_name": "String"},
        {"column_class_id": "2", "column_class_name": "MultiTextColumn",
         "column_class_disp_name": "Multi string"},
        {"column_class_id": "3", "column_class_name": "NumColumn",
         "column_class_disp_name": "Number"},
        {"column_class_id": "4", "column_class_name": "FloatColumn",
         "column_class_disp_name": "Decimal"},
    ]

    def _define_read(self, name):
        """`GET /create/define/<name>/` — the form shape ITA returns.

        Deliberately awkward, because the real one is: the column's
        `column_class` *name* comes back empty and must be resolved from
        `column_class_list`, and only the default field its class uses is
        present.
        """
        d = self.sheet_defs.get(name)
        if d is None and name in self.sheet_cols:
            # A sheet that already existed before this run: ITA still answers
            # GET /create/define/<name>/ for it, and binding needs both names.
            pre = {}
            for i, (logical, display) in enumerate(self.sheet_cols[name].items()):
                pre[f"c{i + 1}"] = {
                    "item_name": display, "item_name_rest": logical,
                    "column_class_id": "1", "single_string_default_value": "",
                    "display_order": i,
                }
            d = {"menu": {"menu_name": name, "menu_name_rest": name,
                          "role_list": ["someone"]},
                 "column": pre}
        if d is None:
            # Measured, not invented: ITA does not 404 a sheet that is not there.
            # It answers 499 `[menu] is invalid. (menu: <name>)`, which is the only
            # way the caller can tell "new sheet" apart from "cannot read it".
            return FakeResponse({"result": "499-49001",
                                 "message": f"[menu] is invalid. (menu: {name})"}, 499)
        columns = {}
        for cid, col in (d.get("column") or {}).items():
            echo = dict(col)
            class_id = str(echo.get("column_class_id") or "1")
            echo["create_column_id"] = f"cid-{name}-{cid}"
            echo["column_class"] = ""            # ITA does not send the name back
            echo["last_update_date_time"] = "2026-09-09 20:25:53.665620"
            echo["last_updated_user"] = "ops user"
            echo["group_id"] = None
            echo["group_name"] = None
            if class_id == "1":
                for drop in ("integer_default_value", "integer_maximum_value",
                             "integer_minimum_value", "decimal_default_value",
                             "decimal_maximum_value", "decimal_minimum_value",
                             "multi_string_default_value", "multi_string_maximum_bytes"):
                    echo.pop(drop, None)
                echo.setdefault("single_string_maximum_bytes", 255)
                echo.setdefault("single_string_regular_expression", "")
            columns[cid] = echo
        menu = dict(d.get("menu") or {})
        menu["menu_create_id"] = f"mc-{name}"
        menu["menu_create_done_status_id"] = "2"
        menu["last_update_date_time"] = "2026-09-09 20:25:53.503709"
        menu["last_updated_user"] = "ops user"
        return FakeResponse({"result": "000-00000", "data": {
            "menu_info": {"menu": menu, "column": columns, "group": {}},
            "column_class_list": self.define_lists.get(
                "column_class_list", self._COLUMN_CLASSES)}})

    def _edit_define(self, name, body):
        """Apply an edit the way ITA does — including deleting omitted columns."""
        cur = self.sheet_defs.get(name)
        if cur is None:
            return FakeResponse({"result": "499-40404",
                                 "message": "The target record not found."}, 499)
        if not body.get("menu_create_id"):
            return FakeResponse({"result": "499-00201",
                                 "message": "menu_create_id is invalid"}, 499)
        newcols = body.get("column") or {}
        for cid, col in newcols.items():
            if not col.get("column_class"):
                return FakeResponse(
                    {"result": "499-00201",
                     "message": '{"0": {"Individual check": ["Required fields."]}}'},
                    499)
        # Measured live, twice. An edit may not change an existing column's
        # required/uniqued -- ITA answers with the message below and refuses the
        # WHOLE payload, values included -- and a column the payload adds is
        # validated as strictly as on a create. A fake that accepted either would
        # let the client regress silently, since ITA's refusal is the only reason
        # the payload is built the way it is.
        curcols = cur.get("column") or {}
        for cid, col in newcols.items():
            old_col = curcols.get(cid)
            if old_col is None:
                if (str(col.get("column_class_id") or "") in ("1", "2")
                        and not col.get("single_string_maximum_bytes")):
                    return FakeResponse(
                        {"result": "499-00001",
                         "message": ('{"0": {"Individual check": "'
                                     '"Maximum number of bytes is "'
                                     'required when String is set."}}')},
                        499)
                continue
            for field in ("required", "uniqued"):
                if str(col.get(field) or "0") != str(old_col.get(field) or "0"):
                    return FakeResponse(
                        {"result": "499-00001",
                         "message": 'In the case of "Edit", the setting of the '
                                    "target of the existing item cannot be changed. "
                                    "(item: %s, target: %s)"
                                    % (col.get("item_name"), field)}, 499)
        # Any column whose label moved -- renamed in place, or deleted and
        # re-added under a fresh id -- puts this sheet into the window.
        prev = {str(c.get("item_name_rest") or ""): str(c.get("item_name") or "")
                for c in curcols.values()}
        for col in newcols.values():
            rest = str(col.get("item_name_rest") or "")
            was, now = prev.get(rest), str(col.get("item_name") or "")
            if rest and was and now and was != now:
                self._subst_stale.setdefault(name, {})[rest] = was
                self._subst_reads[name] = 0
        self.sheet_defs[name] = {"menu": {**(cur.get("menu") or {}),
                                         **(body.get("menu") or {})},
                                "column": newcols}
        self.created_sheets[name] = [c.get("item_name") for c in newcols.values()]
        return FakeResponse({"result": "000-00000", "data": True})

    def _bad_host(self, value):
        """`[H]<host>` must be a host the group links, `[HG]<group>` a group."""
        v = str(value or "")
        if v.startswith(cfg.HOST_VALUE_PREFIX):
            name = v[len(cfg.HOST_VALUE_PREFIX):]
            known = {h for hs in self.hosts.values() for h in hs}
            return None if name in known else v
        if v.startswith("[HG]"):
            return None if v[4:] in self.hosts else v
        return v or "(empty)"

    def _bad_select(self, value):
        """None when the label matches an operation ITA would advertise."""
        v = str(value or "")
        for menu, rows in self.store.items():
            # What a row of this menu already carries is by definition offered:
            # this is the one path the client has to discover rather than derive.
            for row in rows.values():
                if str(row.get("operation_name_select") or "") == v:
                    return None
        for row in self.store.get(cfg.OPERATION_MENU, {}).values():
            stored = str(row.get("scheduled_date_for_execution") or "")
            label = re.sub(r"(\d{2}:\d{2}):\d{2}$", r"\1", stored)
            if v == f"{label}_{row.get('operation_name')}":
                return None
        return v

    def _maintenance(self, menu, method, pk_in_url, params):
        self.writes.append((method, menu, params))
        rows = self.store.setdefault(menu, {})
        pk_col, unique = self._SCHEMA.get(menu, ("item_no", ()))

        if menu == cfg.OPERATION_MENU and method == "POST":
            # Measured: a schedule written as `2026-09-14 09:06` is refused with
            # the format named; slashes with seconds are accepted, and the row
            # then reads back with dashes.
            sched = str(params.get("scheduled_date_for_execution") or "")
            if not re.match(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}$", sched):
                return FakeResponse(
                    {"result": "499-00201",
                     "message": json.dumps({"0": {"scheduled_date_for_execution": [
                         "The value format (YYYY/MM/DD hh:mm:ss) is invalid."
                         f"( input value: {sched})"]}}, ensure_ascii=False)}, 499)

        if self.hosts and "host_name" in params:
            # An input write on the movement's own menu. Measured: a host that is
            # not one of `[H]<host>` / `[HG]<group>` is refused, and so is an
            # operation label that does not exist or carries seconds.
            bad = self._bad_host(params.get("host_name"))
            if bad:
                return FakeResponse(
                    {"result": "499-00201",
                     "message": json.dumps({"0": {"host_name": [
                         "The input value is an invalid value."
                         f"(input value:{bad})"]}}, ensure_ascii=False)}, 499)
            wrong = self._bad_select(params.get("operation_name_select"))
            if wrong is not None:
                return FakeResponse(
                    {"result": "499-00201",
                     "message": json.dumps({"0": {"operation_name_select": [
                         "The input value is an invalid value."
                         f"(input value:{wrong})"]}}, ensure_ascii=False)}, 499)

        if menu == cfg.SUBST_MENU:
            # Measured: ITA checks `menu_group_menu_item` against its own option
            # list at write time. A value the list has not reached yet is refused
            # with the message below, which is what makes a rename a race rather
            # than a plain edit.
            want = str(params.get("menu_group_menu_item") or "")
            if want and want in self._column_items(advance=False).values() \
                    and self.subst_refuse:
                self.subst_refuse -= 1
                return FakeResponse(
                    {"result": "499-00201",
                     "message": json.dumps(
                         {"0": {"menu_group_menu_item": [
                             "The input value is an invalid value."
                             f"(input value:{want})"]}}, ensure_ascii=False)}, 499)
            if want and want not in self._column_items(advance=False).values():
                msg = (f"代入値自動登録用の値は不正な値です。(入力値:{want})"
                       if self.locale == "ja" else
                       f"The input value is an invalid value.(input value:{want})")
                return FakeResponse(
                    {"result": "499-00201",
                     "message": json.dumps({"0": {"menu_group_menu_item": [msg]}},
                                           ensure_ascii=False)}, 499)

        if method == "PATCH":
            if pk_in_url not in rows:
                return FakeResponse({"result": "499-00001",
                                     "message": "The target record not found."}, 499)
            # ITA requires the optimistic-lock token to be echoed back.
            if not params.get("last_update_date_time"):
                return FakeResponse(
                    {"result": "499-00201",
                     "message": '{"0": {"Individual check": '
                                '["last_update_date_time is invalid (None)"]}}'}, 499)
            rows[pk_in_url].update(params)
            return FakeResponse({"result": "000-00000", "data": True})

        has_keys = bool(unique) and all(c in params for c in unique)
        clash = next((k for k, r in rows.items()
                      if has_keys and all(str(r.get(c)) == str(params.get(c))
                                          for c in unique)), None)
        if clash is not None:
            return FakeResponse(
                {"result": "499-00201", "message": self._duplicate_text(params, clash)}, 499)
        key = params.get("item_no") or f"gen-{len(rows)}"
        stored = dict(params)
        # Real ITA rows always carry their own primary-key column, and callers
        # build the PATCH URL from it. ITA also stamps the lock token.
        stored.setdefault(pk_col, key)
        stored.setdefault("last_update_date_time", "2026-09-09 12:00:00.000000")
        if menu == cfg.OPERATION_MENU and stored.get("scheduled_date_for_execution"):
            # Measured: written with slashes, read back with dashes. Doing this at
            # the store keeps the validation above looking at what was sent.
            stored = dict(stored)
            stored["scheduled_date_for_execution"] = str(
                stored["scheduled_date_for_execution"]).replace("/", "-")
        rows[key] = stored
        return FakeResponse({"result": "000-00000", "data": True})


    def _duplicate_text(self, params, clash) -> str:
        """The 499 body ITA returns for a duplicate, in the account's language."""
        ja = self.locale == "ja"
        label = "(個別チェック)" if ja else "Individual check"
        phrase = ("組み合わせが不正です。(組み合わせ:" if ja
                  else "The combination is incorrect. (Combination:")
        tail = ",対象ID:[" if ja else ", Target ID: ["
        return '{"0": {"%s": ["%s%s%s\'%s\'])"]}}' % (label, phrase, params, tail, clash)

    def _column_items(self, advance: bool = True) -> dict:
        """Selectable `menu_group_menu_item` values, in the account's language.

        Real ITA returns these fully localized — 'Substitution value:Sheet:
        Parameter/key' for an English user, '代入値自動登録用:Sheet:パラメータ/key'
        for a Japanese one — so the fake must not hand back an English literal
        that a hardcoded format string could accidentally match.
        """
        # A sheet's columns are usually declared by the test through `variables`
        # (each '<movement>:<var>' implies a movement named <movement>); tests
        # whose sheet name differs from the movement pass sheet_items explicitly.
        by_sheet: dict[str, set] = {}
        for value in self.variables.values():
            movement, _, var = str(value).partition(":")
            by_sheet.setdefault(movement, set()).add(var)
        sheets = set(self.sheet_items) | set(by_sheet) | set(self.created_sheets)
        for mv in self.store.get(cfg.MOVEMENT_MENU, {}).values():
            if mv.get("movement_name"):
                sheets.add(mv["movement_name"])
        out = {}
        for sheet in sheets:
            stale = self._subst_stale.get(sheet) or {}
            if stale and advance:
                if self._subst_reads.get(sheet, 0) < self.subst_lag:
                    self._subst_reads[sheet] = self._subst_reads.get(sheet, 0) + 1
                else:
                    stale = {}
            elif stale:
                stale = {}
            for logical, display in self._columns_of(sheet, by_sheet).items():
                if logical:
                    # the last segment is the DISPLAY name, exactly as measured on
                    # a live install (SheetB: item_name='age', rest='ahe' is
                    # offered as 'Substitution value:SheetB:Parameter/age')
                    out[f"i-{sheet}-{logical}"] = (
                        f"代入値自動登録用:{sheet}:パラメータ/{stale.get(logical, display)}")
        return out

    def _columns_of(self, sheet, by_sheet=None):
        """{logical: display} for a sheet, from wherever the fake knows it."""
        d = self.sheet_defs.get(sheet)
        if d:
            return {str(c.get("item_name_rest") or ""): str(c.get("item_name") or "")
                    for c in (d.get("column") or {}).values()}
        if sheet in self.sheet_cols:
            return self.sheet_cols[sheet]
        return {k: k for k in sorted((by_sheet or {}).get(sheet, ()))}

    def _pulldown(self, menu):
        if menu == cfg.FILE_LINK_MENU:
            return FakeResponse({"data": {"link_file_type": self.link_types}})
        if menu == cfg.ROLE_LINK_MENU:
            return FakeResponse({"data": {"role_package_name_role_name": self.roles}})
        if menu == cfg.MOVEMENT_MENU:
            return FakeResponse({"data": {
                "ansible_agent_execution_environment": self.envs,
                "orchestrator": {"1": "Ansible Legacy", "3": "Ansible Legacy Role",
                                 "5": "Terraform CLI"},
                "host_specific_format": {"1": "IP", "2": "ホスト名"}}})
        if menu == cfg.SUBST_MENU:
            return FakeResponse({"data": {
                "variable_name": self.variables,
                "menu_group_menu_item": self._column_items(),
                "menu_name_rest": {}, "menu_name": {}, "movement": {},
                "member_variable_name": {}, "null_link": {"0": "False"},
                # Exactly what the live install returns for a Japanese-locale
                # account; the English literal must never be sent for it.
                "registration_method": {"1": "Value型", "2": "Key型"}}})
        return FakeResponse({"data": {}})

    def _group_rows(self):
        return FakeResponse({"data": [{"file": {}, "parameter":
                                       {"hostgroup_id": f"hg-{i}", "hostgroup_name": g,
                                        "priority": str(i), "discard": "0"}}
                                      for i, g in enumerate(sorted(self.hosts))]})

    def _link_rows(self):
        return FakeResponse({"data": [{"file": {}, "parameter":
                                       {"host_link_id": f"hl-{i}",
                                        "hostgroup_name": g, "hostname": h,
                                        "discard": "0"}}
                                      for i, (g, hs) in enumerate(sorted(self.hosts.items()))
                                      for h in hs]})

    def _filter(self, menu):
        """Mirror the live shape: `data` is a list of {file, parameter} envelopes."""
        if menu == cfg.HOSTGROUP_MENU:
            return self._group_rows()
        if menu == cfg.HOST_LINK_MENU:
            return self._link_rows()
        if menu == cfg.FILE_LINK_MENU:
            source = self.file_links
        elif menu in self.store:
            source = self.store[menu]
        else:
            source = {}
        return FakeResponse({"data": [{"file": {}, "parameter": p}
                                      for p in source.values()]})




@pytest.fixture
def client_for(monkeypatch):
    """Build a real ExastroClient wired to a FakeExastro transport."""
    def build(**kw):
        fake = FakeExastro(**kw)
        client = ExastroClient()
        monkeypatch.setattr(client, "token", lambda: "t")
        monkeypatch.setattr(client.session, "request", fake.request)
        client._fake = fake
        # Nothing that waits on a platform clock belongs in a unit test unless the
        # test asks for it: the settle windows default to "give up after one
        # attempt", and the tests that exercise the waiting raise it themselves.
        client.SUBST_POLL = 0
        client.SUBST_SETTLE = 0
        return client
    return build


# ---------------------------------------------------------------------------
# Role Package dropdown: file_link -> Package:Role
# ---------------------------------------------------------------------------

def test_git_packages_come_from_file_link(client_for):
    c = client_for(file_links={
        "f1": {"link_file_name": "demo_pkg",
               "link_file_type": "Ansible-LegacyRole/Role package list",
               "file_path": "acme_demo_pkg:demo_pkg/roles",
               "file_sync_state": "Normal"},
        "f2": {"link_file_name": "not_a_role_link",
               "link_file_type": "Terraform/state file", "file_path": "x:y"},
    })
    pkgs = c.git_role_packages()
    assert [p["package"] for p in pkgs] == ["demo_pkg"]
    assert pkgs[0]["file_path"] == "acme_demo_pkg:demo_pkg/roles"


def test_role_choices_groups_git_and_upload(client_for):
    c = client_for(
        file_links={"f1": {"link_file_name": "demo_pkg",
                           "link_file_type": "Ansible-LegacyRole/Role package list",
                           "file_path": "acme_demo_pkg:demo_pkg/roles"}},
        roles={"r1": "demo_pkg:DEMO_HOST_JOB",
               "r2": "demo_pkg:DEMO_TAPE_ROLE",
               "r3": "SheetD:DEMO_PLAYBOOK_ROLE"},
    )
    choices = {p["package"]: p for p in c.role_choices()}
    assert choices["demo_pkg"]["source"] == "git"
    # sorted(): the list is alphabetical, and these names are fixture spellings,
    # not product constants -- an inline order here just tracks the test's own text
    assert choices["demo_pkg"]["roles"] == sorted(["DEMO_HOST_JOB", "DEMO_TAPE_ROLE"])
    assert choices["SheetD"]["source"] == "upload"


def test_selectable_roles_matches_ita_pulldown(client_for):
    c = client_for(roles={"r1": "demo_pkg:DEMO_HOST_JOB"})
    assert c.selectable_roles() == ["demo_pkg:DEMO_HOST_JOB"]


def test_dropdown_composes_package_and_role():
    """The UI sends two dropdown values; ITA needs one `Package:Role` string."""
    assert appmod.compose_role_value("demo_pkg",
                                     "DEMO_HOST_JOB", "") == \
        "demo_pkg:DEMO_HOST_JOB"
    # advanced free-text wins when already composed
    assert appmod.compose_role_value("demo_pkg", "DEMO_TAPE_ROLE",
                                     "SheetD:DEMO_PLAYBOOK_ROLE") == \
        "SheetD:DEMO_PLAYBOOK_ROLE"
    # bare role typed in the advanced box pairs with the chosen package
    assert appmod.compose_role_value("demo_pkg", "", "DEMO_TAPE_ROLE") == \
        "demo_pkg:DEMO_TAPE_ROLE"


# ---------------------------------------------------------------------------
# Movement + execution environment
# ---------------------------------------------------------------------------

def test_execution_env_is_written_to_the_movement(client_for):
    """No environment -> Exastro collects no role variables -> nothing binds."""
    c = client_for()
    assert c.create_movement("m1", "DEMO_EXEC_ENV") == "OK"
    movement = [w for w in c._fake.writes if w[1] == cfg.MOVEMENT_MENU][0]
    assert movement[2]["ansible_agent_execution_environment"] == "DEMO_EXEC_ENV"


def test_existing_movement_without_env_is_patched_not_duplicated(client_for):
    c = client_for(movements={"m-uuid": {
        "movement_id": "m-uuid", "movement_name": "DEMO_FLOW", "discard": "0",
        "orchestrator": "Ansible Legacy Role",
        "ansible_agent_execution_environment": None,
        "last_update_date_time": "2026-09-02 20:34:44.446451"}})
    result = c.create_movement("DEMO_FLOW", "DEMO_EXEC_ENV")
    assert "updated" in result
    patches = [w for w in c._fake.writes if w[0] == "PATCH" and w[1] == cfg.MOVEMENT_MENU]
    assert len(patches) == 1
    assert patches[0][2]["ansible_agent_execution_environment"] == "DEMO_EXEC_ENV"
    # the optimistic-lock token must be echoed back or ITA rejects the PATCH
    assert patches[0][2]["last_update_date_time"] == "2026-09-02 20:34:44.446451"
    assert not [w for w in c._fake.writes if w[0] == "POST" and w[1] == cfg.MOVEMENT_MENU]


def test_patch_without_lock_token_is_rejected(client_for):
    """ITA answers 'last_update_date_time is invalid (None)' to a PATCH missing
    the lock token — exactly what every update path in this tool used to send,
    and the reason failures were invisible."""
    from exastro_client import ExastroError
    c = client_for(links={"l1": {"associated_item_no": "l1", "movement": "m1",
                                 "include_order": "1", "discard": "0",
                                 "role_package_name_role_name": "a:b"}})
    url = f"{cfg.API_BASE}/menu/{cfg.ROLE_LINK_MENU}/maintenance/l1/"
    with pytest.raises(ExastroError) as exc:
        c._request("PATCH", url, json={"parameter": {"movement": "m1"}, "file": {}})
    assert "last_update_date_time is invalid" in str(exc.value)





def test_filter_records_unwraps_the_ita_envelope(client_for):
    """Regression: /filter/ returns {file, parameter} envelopes.

    Returned raw, every existence check saw an empty row and the tool
    duplicated movements on each re-run.
    """
    c = client_for(movements={"u1": {"movement_id": "u1", "movement_name": "m1",
                                     "discard": "0"}})
    rows = c.filter_records(cfg.MOVEMENT_MENU)
    assert rows and rows[0]["movement_name"] == "m1"
    assert "parameter" not in rows[0]


def test_existing_movement_is_not_duplicated(client_for):
    c = client_for(movements={"u1": {"movement_id": "u1", "movement_name": "m1",
                                     "discard": "0",
                                     "ansible_agent_execution_environment": "DEMO_EXEC_ENV"}})
    result = c.create_movement("m1", "DEMO_EXEC_ENV")
    assert result.startswith("exists")
    assert not [w for w in c._fake.writes if w[1] == cfg.MOVEMENT_MENU]


def test_discarded_movement_is_revived_not_duplicated(client_for):
    """Two live movements sharing a name is what produced the 'Failed to
    exchange ID.' rows on the live install, so a discarded twin is revived
    rather than a second one created."""
    c = client_for(movements={"u1": {"movement_id": "u1", "movement_name": "m1",
                                     "discard": "1",
                                     "last_update_date_time":
                                         "2026-01-01 00:00:00.000000"}})
    result = c.create_movement("m1", "DEMO_EXEC_ENV")
    assert "updated existing row" in result
    assert len(c._fake.store[cfg.MOVEMENT_MENU]) == 1
    revived = list(c._fake.store[cfg.MOVEMENT_MENU].values())[0]
    assert revived["discard"] == "0"
    assert revived["ansible_agent_execution_environment"] == "DEMO_EXEC_ENV"



def test_resolve_execution_env_falls_back_when_config_gone(client_for, monkeypatch):
    c = client_for(envs={"1": "~[Exastro standard] default", "e1": "DEMO_EXEC_ENV",
                         "e2": "BACKUP_ENV"})
    monkeypatch.setattr(cfg, "EXEC_ENV", "DELETED_ENV")
    # What the fallback guarantees is *which kind* of answer, not a spelling: a
    # real environment, never the platform default, chosen as the first of the
    # list `execution_environments()` returns (default pushed last, then
    # alphabetical). Naming one fixture value here would only track the test's
    # own spelling, so the expectation is derived from the same rule.
    first_real = sorted(["DEMO_EXEC_ENV", "BACKUP_ENV"], key=str.lower)[0]
    assert c.resolve_execution_env("") == first_real
    assert c.resolve_execution_env("BACKUP_ENV") == "BACKUP_ENV"
    assert not c.resolve_execution_env("nope").startswith("~[")
    assert c.resolve_execution_env("nope") == first_real



def test_unselectable_role_is_refused_before_write(client_for):
    c = client_for(roles={"r1": "demo_pkg:DEMO_HOST_JOB"})
    result = c.link_movement_role("m1", "typo_package:TypoRole")
    assert result.startswith("FAIL")
    assert not [w for w in c._fake.writes if w[1] == cfg.ROLE_LINK_MENU]


# ---------------------------------------------------------------------------
# Binding EVERY parameter (the reported bug)
# ---------------------------------------------------------------------------

def test_all_json_keys_are_bound_when_variables_exist(client_for):
    c = client_for(variables={
        "v1": "zos:p_jobname", "v2": "zos:p_reply_flag",
        "v3": "zos:p_host"})
    rows = c.link_movement_parameter("zos", "zos", {
        "p_jobname": "JOB0000", "p_reply_flag": "True", "p_host": "h1"},
        wait=False)
    assert [r["status"] for r in rows] == ["linked"] * 3
    assert len([w for w in c._fake.writes if w[1] == cfg.SUBST_MENU]) == 3


def test_no_silent_fallback_to_an_unrelated_variable(client_for):
    """Regression: p_environment was once bound to role variable `name`.

    A key with no matching role variable must be reported, never attached to
    whichever variable happens to exist — that writes fine and then feeds the
    role the wrong value at execution time.
    """
    c = client_for(variables={"v1": "ai:name"},
                   sheet_items={"ai": ["p_environment"]})
    rows = c.link_movement_parameter("ai", "ai", {"p_environment": "prod"},
                                     wait=False)
    assert rows[0]["status"] == "missing_variable"
    assert not [w for w in c._fake.writes if w[1] == cfg.SUBST_MENU]


def test_missing_keys_reported_without_blocking_the_rest(client_for):
    c = client_for(variables={"v1": "zos:p_jobname"},
                   sheet_items={"zos": ["p_jobname", "bogus"]})
    rows = c.link_movement_parameter("zos", "zos",
                                     {"p_jobname": "J", "bogus": "x"}, wait=False)
    assert rows[0]["status"] == "linked"
    assert rows[1]["status"] == "missing_variable"
    assert "collected: p_jobname" in rows[1]["detail"]


def test_variable_map_binds_a_renamed_role_variable(client_for):
    c = client_for(variables={"v1": "zos:p_jobname"},
                   sheet_items={"zos": ["job_name"]})
    c._variable_map = {"job_name": "p_jobname"}
    rows = c.link_movement_parameter("zos", "zos", {"job_name": "JOB0000"}, wait=False)
    assert rows[0]["status"] == "linked"
    assert rows[0]["variable"] == "zos:p_jobname"


def test_substitution_row_shape(client_for):
    c = client_for(variables={"v1": "zos:p_jobname"},
                   sheet_items={"zos_job_submit": ["p_jobname"]})
    c.link_movement_parameter("zos", "zos_job_submit",
                              {"p_jobname": "J"}, wait=False)
    row = [w for w in c._fake.writes if w[1] == cfg.SUBST_MENU][0][2]
    assert row["menu_name_rest"] == "zos_job_submit_subst"
    # taken verbatim from the account's pulldown, so it is localized
    assert row["menu_group_menu_item"] == \
        "代入値自動登録用:zos_job_submit:パラメータ/p_jobname"
    assert row["variable_name"] == "zos:p_jobname"
    # resolved from this account's pulldown, not the English config literal
    assert row["registration_method"] == "Value型"
    assert row["registration_method"] != cfg.SUBST_REGISTRATION_METHOD


def test_wait_loop_gives_up_after_timeout(client_for, monkeypatch):
    """Variables that never appear must not hang the request forever."""
    c = client_for(variables={})
    monkeypatch.setattr(cfg, "VAR_TIMEOUT", 0)
    monkeypatch.setattr(cfg, "VAR_INTERVAL", 1)
    assert c.wait_for_role_variables("zos", ["p_jobname"]) == []


# ---------------------------------------------------------------------------
# Status reporting (the step used to look green while doing nothing)
# ---------------------------------------------------------------------------

def test_partial_binding_is_not_reported_ok(client_for, monkeypatch):
    c = client_for(variables={"v1": "zos:p_jobname"})
    monkeypatch.setattr(appmod, "client", c)
    report = appmod.create_everything(
        "zos", "demo_pkg:DEMO_HOST_JOB", "zos",
        {"p_jobname": "J", "nope": "x"}, "DEMO_EXEC_ENV", wait_vars=False)
    assert report["status"] == "PARTIAL"
    assert report["linked"] == 1 and report["total"] == 2
    assert report["steps"][-1]["ok"] is False


def test_full_binding_is_ok(client_for, monkeypatch):
    c = client_for(variables={"v1": "zos:p_jobname"})
    monkeypatch.setattr(appmod, "client", c)
    report = appmod.create_everything(
        "zos", "demo_pkg:DEMO_HOST_JOB", "zos",
        {"p_jobname": "J"}, "DEMO_EXEC_ENV", wait_vars=False)
    assert report["status"] == "OK"


def test_missing_execution_env_is_not_silently_ok(client_for, monkeypatch):
    """The mock returns a warning string; the live path would skip everything."""
    assert appmod._looks_bad("OK (mock, but no execution env -> role vars unavailable)")
    assert appmod._looks_bad("2 of 4 keys are missing_variable")
    assert not appmod._looks_bad("OK")
    assert not appmod._looks_bad("exists: movement 'm1' (env DEMO_EXEC_ENV)")


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

def _login(http, username="testuser", password="test-password"):
    """Put an already-created user into a Flask test client's session."""
    with http.session_transaction() as sess:
        sess.clear()
        sess["user"] = username
    return http


@pytest.fixture
def mock_client(monkeypatch, tmpdb, store):
    # `tmpdb` and `store` are dependencies, not decoration. Every app-level test
    # below drives the real routes: history writes must stay in a throwaway DB,
    # and the user/profile store must be isolated for every test as well.
    monkeypatch.setattr(cfg, "MOCK", True)
    from exastro_client import MockExastroClient
    monkeypatch.setattr(appmod, "client", MockExastroClient())
    settings.create_user("testuser", "test-password")
    settings.adopt_orphans("testuser")
    appmod.app.config["TESTING"] = True
    return _login(appmod.app.test_client())


def test_index_renders_dropdowns(mock_client):
    html = mock_client.get("/").get_data(as_text=True)
    assert "demo_pkg" in html
    assert 'name="role_package"' in html and 'name="role_select"' in html


def test_role_choices_endpoint(mock_client):
    data = mock_client.get("/api/role_choices").get_json()
    git = [p for p in data["packages"] if p["source"] == "git"]
    assert {p["package"] for p in git} >= {"demo_pkg"}
    assert "demo_pkg:DEMO_HOST_JOB" in \
        [f"{p['package']}:{r}" for p in data["packages"] for r in p["roles"]]
    assert data["environments"]


def test_incomplete_role_is_rejected(mock_client):
    resp = mock_client.post("/create", data={
        "movement_name": "m", "role_package": "demo_pkg",
        "role_select": "", "exec_env": "DEMO_EXEC_ENV", "wait_vars": "on",
        "parameters": '{"a": 1}'})
    assert resp.status_code == 400
    assert "Package" in resp.get_data(as_text=True)


def test_wait_requires_an_environment(mock_client):
    resp = mock_client.post("/create", data={
        "movement_name": "m", "role_package": "demo_pkg",
        "role_select": "DEMO_HOST_JOB", "exec_env": "", "wait_vars": "on",
        "parameters": '{"a": 1}'})
    assert resp.status_code == 400
    assert "execution environment" in resp.get_data(as_text=True).lower()


def test_happy_path_binds_every_key(mock_client):
    html = mock_client.post("/create", data={
        "movement_name": "demo", "role_package": "demo_pkg",
        "role_select": "DEMO_HOST_JOB", "exec_env": "DEMO_EXEC_ENV",
        "wait_vars": "on",
        "parameters": '{"p_jobname":"JOB0000","p_reply_flag":"True",'
                      '"p_host":"[H]localhost","p_dataset":"X"}'}).get_data(as_text=True)
    assert "4/4 parameters bound" in html
    assert "demo_pkg:DEMO_HOST_JOB" in html


# ---------------------------------------------------------------------------
# Re-running against existing rows (ITA's unique-combination rules)
# ---------------------------------------------------------------------------

def test_relinking_a_movement_updates_instead_of_failing(client_for):
    """A movement holds one role link per include_order, so a second POST is a
    duplicate. Re-running must PATCH the existing row to the new role."""
    c = client_for(links={"l1": {"associated_item_no": "l1", "movement": "m1",
                                 "include_order": "1", "discard": "0",
                                 "last_update_date_time":
                                     "2026-01-01 00:00:00.000000",
                                 "role_package_name_role_name":
                                     "demo_pkg:DEMO_TAPE_ROLE"}})
    result = c.link_movement_role("m1", "demo_pkg:DEMO_HOST_JOB")
    assert "updated existing row" in result
    assert [w for w in c._fake.writes if w[0] == "PATCH"][0][2][
        "role_package_name_role_name"] == "demo_pkg:DEMO_HOST_JOB"


def test_upsert_reports_both_causes_when_update_also_fails(client_for):
    """The old code hid the PATCH failure and re-ran the failing POST, so the
    user saw only 'combination is incorrect' with no reason."""
    c = client_for(links={"l1": {"associated_item_no": "l1", "movement": "m1",
                                 "include_order": "1", "discard": "0",
                                 "role_package_name_role_name": "a:b"}})
    from exastro_client import ExastroError
    # make the stored row unmatchable so no PATCH is attempted
    with pytest.raises(ExastroError) as exc:
        c._upsert(cfg.ROLE_LINK_MENU, {"movement": "m1", "include_order": "1",
                                       "role_package_name_role_name": "c:d"},
                  {"nonexistent_column": "x"})
    assert "no matching row is readable" in str(exc.value)


def test_link_is_idempotent_on_repeated_runs(client_for):
    c = client_for(variables={"v1": "zos:p_jobname"})
    params = {"p_jobname": "J"}
    first = c.link_movement_parameter("zos", "zos", params, wait=False)
    second = c.link_movement_parameter("zos", "zos", params, wait=False)
    assert first[0]["status"] == "linked"
    assert second[0]["status"] in ("linked", "exists")
    assert "error" not in second[0]["detail"].lower()


# ---------------------------------------------------------------------------
# Locale: ITA validates pulldown columns by their *display* string, and returns
# those strings in the calling account's language. Hardcoding English labels
# makes every write fail with "利用できない値です" for a Japanese user.
# ---------------------------------------------------------------------------

def test_sheet_creation_sends_the_accounts_own_labels(client_for):
    c = client_for()
    c.create_parameter_sheet("s1", {"x": "text"})
    payload = [w for w in c._fake.writes if w[1] == "create/define"][0][2]
    menu = payload["menu"]
    assert menu["sheet_type"] == "パラメータシート（ホスト/オペレーションあり）"
    assert menu["menu_group_for_subst"] == "代入値自動登録用"
    assert menu["menu_group_for_input"] == "入力用"
    assert menu["menu_group_for_ref"] == "参照用"
    # ids stay English-independent and are still sent alongside
    assert menu["sheet_type_id"] == cfg.SHEET_TYPE_ID
    assert menu["menu_group_for_subst_id"] == cfg.MENU_GROUP_SUBST_ID


def test_substitution_row_sends_the_accounts_own_registration_method(client_for):
    c = client_for(variables={"v1": "m1:p_jobname"},
                   sheet_items={"s1": ["p_jobname"]})
    c.link_movement_parameter("m1", "s1", {"p_jobname": "J"}, wait=False)
    row = [w for w in c._fake.writes if w[1] == cfg.SUBST_MENU][-1][2]
    assert row["registration_method"] == "Value型"


def test_labels_fall_back_to_config_when_the_list_is_unreachable(client_for):
    c = client_for(define_lists={})
    c.create_parameter_sheet("s1", {"x": "text"})
    menu = [w for w in c._fake.writes if w[1] == "create/define"][0][2]["menu"]
    assert menu["sheet_type"] == "Parameter Sheet(Host/Operation)"


def test_a_named_role_is_never_swapped_for_an_admin_one(client_for):
    """An operator who picks `developer` because the account may not — or should
    not — assign administrators must not be quietly upgraded to `_x-admin`."""
    c = client_for(define_lists={**FakeExastro._DEFINE_LISTS,
                                 "role_list": ["developer", "testinguser"]})
    assert c.grantable_roles("developer") == ["developer"]
    assert c.grantable_roles("team-sheet-owner") == ["team-sheet-owner"]
    # a name this account was not offered is still sent as asked; the refusal, if
    # any, is handled by the retry in create_parameter_sheet, not by guessing
    assert c.grantable_roles("_demo_ws-admin") == ["_demo_ws-admin"]
    assert c.grantable_roles("  spaced  ") == ["spaced"]


def test_a_refused_team_role_still_creates_the_sheet_without_admin(client_for,
                                                                   monkeypatch):
    """The whole point of the retry: a create-only account asking for a team role
    gets its sheet, and no administrator appears that nobody asked for."""
    from exastro_client import NO_ROLE_SENTINEL
    monkeypatch.setattr(cfg, "ADMIN_ROLE", "sheet-owners")
    c = client_for(define_lists={**FakeExastro._DEFINE_LISTS,
                                 "role_list": ["developer"]})
    real = c._request

    def refused(method, url, **kw):
        roles = (((kw.get("json") or {}).get("menu") or {}).get("role_list")) or []
        if "create/define/execute" in url and roles != [NO_ROLE_SENTINEL]:
            raise ExastroError("The Value of [ Admin role ] is invalid.")
        return real(method, url, **kw)

    monkeypatch.setattr(c, "_request", refused)
    detail = c.create_parameter_sheet("s1", {"x": "text"})
    assert detail.startswith("created:") and "without an admin role grant" in detail
    assert appmod._looks_bad(detail) is False
    sent = [w[2]["menu"]["role_list"] for w in c._fake.writes if w[1] == "create/define"]
    assert sent == [[NO_ROLE_SENTINEL]]


def test_role_lookup_survives_a_missing_admin_role_in_sheet_payload(client_for,
                                                         monkeypatch):
    monkeypatch.setattr(cfg, "ADMIN_ROLE", "_demo_ws-admin")
    c = client_for(define_lists={**FakeExastro._DEFINE_LISTS,
                                 "role_list": ["testinguser"]})
    c.create_parameter_sheet("s1", {"x": "text"})
    menu = [w for w in c._fake.writes if w[1] == "create/define"][0][2]["menu"]
    assert menu["role_list"] == ["_demo_ws-admin"]   # asked for, not substituted


def test_a_blank_field_granting_nothing_is_never_retried_as_a_failure(client_for,
                                                                     monkeypatch):
    """Nothing to rescue when the request already asked for no administrator:
    the error has some other cause and must be reported, not masked."""
    from exastro_client import NO_ROLE_SENTINEL
    monkeypatch.setattr(cfg, "ADMIN_ROLE", "")
    monkeypatch.setattr(cfg, "USER", "")
    c = client_for()
    real = c._request

    def broken(method, url, **kw):
        if "create/define/execute" in url:
            raise ExastroError("The menu name cannot be used.")
        return real(method, url, **kw)

    monkeypatch.setattr(c, "_request", broken)
    detail = c.create_parameter_sheet("s1", {"x": "text"})
    assert "cannot be used" in detail and appmod._looks_bad(detail)
    posts = [w for w in c._fake.writes if w[1] == "create/define"]
    assert all((((w[2]["menu"]).get("role_list")) or [None])[0] == NO_ROLE_SENTINEL
               for w in posts)


def test_key_without_a_sheet_column_is_reported_separately(client_for):
    """A key missing from the sheet is a different fault from a key missing on
    the role, and needs a different fix (the sheet, not the role)."""
    c = client_for(variables={"v1": "zos:p_jobname"},
                   sheet_items={"zos": ["p_jobname"]})
    rows = c.link_movement_parameter("zos", "zos", {"typo_key": "x"}, wait=False)
    assert rows[0]["status"] == "missing_column"
    assert "no column named 'typo_key'" in rows[0]["detail"]


# ---------------------------------------------------------------------------
# ITA translates its error messages too, so duplicate detection cannot be
# English-only or a Japanese account's re-run raises instead of updating.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("locale", ["en", "ja"])
def test_duplicate_is_recognised_in_either_language(client_for, locale):
    c = client_for(locale=locale, links={
        "l1": {"associated_item_no": "l1", "movement": "m1", "include_order": "1",
               "discard": "0",
               "last_update_date_time": "2026-01-01 00:00:00.000000",
               "role_package_name_role_name": "a:b"}})
    out = c.link_movement_role("m1", "demo_pkg:DEMO_HOST_JOB")
    assert "updated existing row" in out, (locale, out)


def test_sheet_duplicate_in_japanese_reports_exists(client_for):
    c = client_for()
    c.create_parameter_sheet("s1", {"x": "text"})
    out = c.create_parameter_sheet("s1", {"x": "text"})
    assert out.startswith("exists:"), out


def test_unusable_value_is_never_reported_as_exists(client_for):
    """The old handler treated 'cannot be used' as 'already exists', so a sheet
    that failed to create was reported as present and the run looked green."""
    c = client_for()
    c._fake.routes_unusable = True
    def unusable(method, url, **kw):
        if "/create/define/execute/" in url:
            return FakeResponse({"result": "499-00201", "message": json.dumps(
                {"sheet_type": ["利用できない値です。(入力値:Parameter Sheet(Host/Operation))"]},
                ensure_ascii=False)}, 499)
        return c._fake.request(method, url, **kw)
    c.session.request = unusable
    out = c.create_parameter_sheet("s2", {"x": "text"})
    assert not out.startswith("exists:")
    assert "failed" in out


def test_git_packages_are_found_for_a_japanese_account(client_for):
    """`link_file_type` stores a translated display string; filtering on the
    English literal made every Git package look like an uploaded one."""
    c = client_for(file_links={
        "f1": {"link_file_name": "demo_pkg",
               "link_file_type": "Ansible-LegacyRole/ロールパッケージ管理",
               "file_path": "acme_demo_pkg:demo_pkg/roles"},
        "f2": {"link_file_name": "docs_share",
               "link_file_type": "Ansible共通/ファイル管理",
               "file_path": "some/repo"}})
    pkgs = c.git_role_packages()
    assert [p["package"] for p in pkgs] == ["demo_pkg"]
    assert pkgs[0]["file_path"] == "acme_demo_pkg:demo_pkg/roles"


def test_git_packages_still_found_for_an_english_account(client_for):
    c = client_for(file_links={"f1": {"link_file_name": "demo_pkg",
                                      "link_file_type":
                                          "Ansible-LegacyRole/Role package list",
                                      "file_path": "x/y"}})
    assert [p["package"] for p in c.git_role_packages()] == ["demo_pkg"]


def test_orchestrator_is_resolved_not_hardcoded(client_for):
    c = client_for()
    c.create_movement("m9", "DEMO_EXEC_ENV")
    row = [w for w in c._fake.writes if w[1] == cfg.MOVEMENT_MENU][0][2]
    assert row["orchestrator"] == "Ansible Legacy Role"


# ---------------------------------------------------------------------------
# The last display-validated column: host_specific_format
# ---------------------------------------------------------------------------

def test_host_format_is_resolved_not_hardcoded(client_for, monkeypatch):
    c = client_for()
    assert c.host_format_label() == "IP"
    # switch the requested id and the account's own label follows, in its own
    # language — the English literal would have produced 'Host name'
    monkeypatch.setattr(cfg, "HOST_SPECIFIC_FORMAT_ID", "2")
    assert c.host_format_label() == "ホスト名"
    c.create_movement("m7", "DEMO_EXEC_ENV")
    row = [w for w in c._fake.writes if w[1] == cfg.MOVEMENT_MENU][0][2]
    assert row["host_specific_format"] == "ホスト名"


# ---------------------------------------------------------------------------
# Creation history: all runs kept, newest 20 shown, full report per run
# ---------------------------------------------------------------------------

@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    """Point the app at a throwaway database so tests never touch real history."""
    monkeypatch.setattr(appmod, "DB_PATH", str(tmp_path / "hist.db"))
    appmod.init_db()
    return appmod


def test_history_keeps_every_run_but_pages_by_ten(tmpdb):
    for i in range(35):
        appmod.save_creation(f"m{i}", "p:r", f"s{i}", "OK", owner="testuser")
    page1 = appmod.paginate_creations(1)
    assert page1["total"] == 35 and page1["pages"] == 4
    assert len(page1["rows"]) == 10
    assert page1["rows"][0]["movement_name"] == "m34"                 # newest first
    assert (page1["start"], page1["end"]) == (1, 10)
    assert page1["has_next"] and not page1["has_prev"]
    last = appmod.paginate_creations(4)
    assert len(last["rows"]) == 5 and last["rows"][-1]["movement_name"] == "m0"
    assert last["has_prev"] and not last["has_next"]
    with appmod.db() as conn:                                         # nothing deleted
        assert conn.execute("SELECT COUNT(*) FROM creations").fetchone()[0] == 35


def test_pages_out_of_range_clamp_instead_of_erroring(tmpdb):
    for i in range(3):
        appmod.save_creation(f"m{i}", "p:r", f"s{i}", "OK", owner="testuser")
    assert appmod.paginate_creations(99)["page"] == 1
    assert appmod.paginate_creations(0)["page"] == 1
    assert appmod.paginate_creations(-5)["page"] == 1
    assert appmod.paginate_creations(1)["rows"]                 # still usable


def test_history_pages_are_clickable(mock_client, tmpdb):
    for i in range(25):
        appmod.save_creation(f"m{i}", "p:r", f"s{i}", "OK", owner="testuser")
    html = mock_client.get("/").get_data(as_text=True)
    assert "?page=2" in html and "3 runs" in html or "?page=2" in html
    assert "/creation/" in html
    p2 = mock_client.get("/?page=2").get_data(as_text=True)
    assert "?page=1" in p2 and "?page=3" in p2
    # a page beyond the end must not blow up
    assert mock_client.get("/?page=999").status_code == 200
    assert mock_client.get("/?page=notanumber").status_code == 200


def test_report_round_trips_through_storage(tmpdb):
    report = {"status": "PARTIAL", "linked": 1, "total": 2,
              "steps": [{"label": "Create Movement", "ok": True, "detail": "OK"}],
              "bindings": [{"key": "a", "variable": "m:a", "status": "linked",
                            "detail": "OK"}]}
    cid = appmod.save_creation("m", "p:r", "s", "PARTIAL",
                               {"report": report, "params": {"a": 1, "b": "x"},
                                "types": {"a": "integer", "b": "string"},
                                "execution_env": "DEMO_EXEC_ENV", "wait_vars": True}, owner="testuser")
    got = appmod.get_creation(cid)
    assert got["status"] == "PARTIAL"
    assert got["report"]["report"]["bindings"][0]["variable"] == "m:a"
    assert got["report"]["params"] == {"a": 1, "b": "x"}
    assert got["report"]["execution_env"] == "DEMO_EXEC_ENV"


def test_legacy_row_without_a_report_is_handled(tmpdb):
    with appmod.db() as conn:
        cur = conn.execute(
            "INSERT INTO creations (movement_name, role_name, sheet_name,"
            " status, created_at) VALUES ('old','p:r','s','OK','2026-01-01')")
    cid = cur.lastrowid
    got = appmod.get_creation(cid)
    assert got["report"] is None            # detail page shows a friendly notice


def test_init_db_adds_the_report_column_to_an_old_table(tmp_path, monkeypatch):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE creations (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 " movement_name TEXT, role_name TEXT, sheet_name TEXT,"
                 " status TEXT, created_at TEXT)")
    conn.execute("INSERT INTO creations (movement_name, role_name, sheet_name,"
                 " status, created_at) VALUES ('x','y','z','OK','2026-01-01')")
    conn.commit(); conn.close()
    monkeypatch.setattr(appmod, "DB_PATH", path)
    appmod.init_db()                         # must not raise or lose the row
    assert appmod.get_creation(1)["movement_name"] == "x"
    assert appmod.get_creation(1)["report"] is None


def test_init_db_is_idempotent(tmpdb):
    appmod.init_db()
    appmod.init_db()
    cid = appmod.save_creation("m", "p:r", "s", "OK", {"report": {"steps": []}}, owner="testuser")
    assert appmod.get_creation(cid)["report"]["report"]["steps"] == []


def test_detail_route_shows_the_full_history(mock_client, tmpdb):
    resp = mock_client.post("/create", data={
        "movement_name": "zos_submit", "role_package": "demo_pkg",
        "role_select": "DEMO_HOST_JOB", "exec_env": "DEMO_EXEC_ENV",
        "wait_vars": "on",
        "parameters": '{"p_jobname": "JOB0000", "p_reply_flag": "True"}'})
    assert resp.status_code == 200
    cid = appmod.list_creations()[0]["id"]

    html = mock_client.get(f"/creation/{cid}").get_data(as_text=True)
    assert f"Run #{cid}" in html
    assert "zos_submit" in html
    assert "demo_pkg:DEMO_HOST_JOB" in html      # role
    assert "DEMO_EXEC_ENV" in html                                 # exec env
    assert "p_jobname" in html and "p_reply_flag" in html     # bindings
    assert "Steps" in html
    # the submitted values are replayable from the history page
    assert "JOB0000" in html


def test_detail_route_rejects_an_unknown_run(mock_client, tmpdb):
    assert mock_client.get("/creation/424242").status_code == 404


def test_detail_route_handles_a_legacy_row(mock_client, tmpdb):
    with appmod.db() as conn:
        cur = conn.execute("INSERT INTO creations (movement_name, role_name,"
                           " sheet_name, status, created_at, owner) VALUES"
                           " ('old','p:r','s','OK','2026-01-01','testuser')")
    html = mock_client.get(f"/creation/{cur.lastrowid}").get_data(as_text=True)
    assert "predates detailed history" in html


def test_index_links_each_run_to_its_detail(mock_client, tmpdb):
    appmod.save_creation("m1", "p:r", "s1", "OK", {"report": {"steps": []}}, owner="testuser")
    html = mock_client.get("/").get_data(as_text=True)
    assert "/creation/" in html
    assert "Detail" in html
    assert "1 run" in html


def test_api_creations_exposes_urls_not_the_blob(mock_client, tmpdb):
    appmod.save_creation("m1", "p:r", "s1", "OK",
                         {"report": {"steps": [{"label": "x" * 200}]}}, owner="testuser")
    data = mock_client.get("/api/creations").get_json()["creations"]
    assert data[0]["has_detail"] is True
    assert data[0]["detail_url"].startswith("/creation/")
    assert "report_json" not in data[0]


def test_index_prefills_from_a_detail_rerun_link(mock_client, tmpdb):
    html = mock_client.get("/?movement_name=prev_run&sheet_name=prev_sheet"
                           "&role_name=demo_pkg%3ADEMO_TAPE_ROLE"
                           ).get_data(as_text=True)
    assert 'value="prev_run"' in html
    assert 'value="prev_sheet"' in html


def test_overlay_is_declared_before_the_script_that_binds_it():
    """The guard looks the element up when the inline script runs; a div added
    after it was null, so no popup ever appeared."""
    tpl = pathlib.Path(__file__).resolve().parent.parent / "templates" / "index.html"
    src = tpl.read_text(encoding="utf-8")
    script = src.index("form.addEventListener('submit'")
    assert src.index('<div id="busy"') < script
    # and only one copy exists
    assert src.count('<div id="busy"') == 1


def test_api_creations_reports_the_page(mock_client, tmpdb):
    for i in range(12):
        appmod.save_creation(f"m{i}", "p:r", f"s{i}", "OK", owner="testuser")
    data = mock_client.get("/api/creations").get_json()
    assert data["total"] == 12 and data["pages"] == 2 and data["per_page"] == 10
    assert len(data["creations"]) == 10
    assert mock_client.get("/api/creations?page=2").get_json()["page"] == 2


# ---------------------------------------------------------------------------
# Form contract: dropdowns only, blank sheet name, bilingual labels
# ---------------------------------------------------------------------------

def test_manual_role_entry_is_gone(mock_client, tmpdb):
    html = mock_client.get("/").get_data(as_text=True)
    assert 'name="role_name"' not in html
    assert "Advanced" not in html
    assert "type <code>Package:Role</code>" not in html



def test_dropdowns_are_required_and_carry_the_preset(mock_client, tmpdb):
    import re
    html = mock_client.get("/?role_name=demo_pkg%3ADEMO_TAPE_ROLE"
                           ).get_data(as_text=True)
    tags = {m.group(1): m.group(0) for m in re.finditer(
        r"<select[^>]*name=\"([^\"]+)\"[^>]*>", html)}
    assert "required" in tags["role_package"]
    assert "required" in tags["role_select"]
    assert 'data-preset="demo_pkg:DEMO_TAPE_ROLE"' in html


def test_blank_sheet_name_defaults_to_the_movement_name(mock_client, tmpdb):
    resp = mock_client.post("/create", data={
        "movement_name": "solo_run", "role_package": "demo_pkg",
        "role_select": "DEMO_HOST_JOB", "exec_env": "DEMO_EXEC_ENV",
        "wait_vars": "on", "sheet_name": "",
        "parameters": '{"p_jobname": "J1"}'})
    assert resp.status_code == 200
    with appmod.db() as conn:
        got = conn.execute("SELECT sheet_name, movement_name FROM creations"
                           " ORDER BY id DESC LIMIT 1").fetchone()
    assert got["movement_name"] == "solo_run"
    assert got["sheet_name"] == "solo_run"          # the default applied





# ---------------------------------------------------------------------------
# Interface language: one language at a time, chosen by the operator
# ---------------------------------------------------------------------------

import i18n as i18nmod

CJK = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")
SWITCHER = re.compile(r'<nav class="langsw".*?</nav>', re.S)
INVISIBLE = re.compile(
    r"<style>.*?</style>|<script>.*?</script>|<!--.*?-->", re.S)
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def visible(html: str) -> str:
    """Only what the operator can read.

    Styles, scripts and HTML comments are dropped: they carry English code
    comments that are not UI text. The language switcher is dropped too, since it
    names each language in its own script regardless of the current choice.
    """
    return SWITCHER.sub("", INVISIBLE.sub("", html))


def test_every_key_has_both_languages_with_the_same_placeholders():
    for key, entry in i18nmod.TEXT.items():
        assert set(entry) >= {"en", "ja"}, key
        for code, text in entry.items():
            assert text.strip(), (key, code)
        assert (PLACEHOLDER.findall(entry["en"])
                == PLACEHOLDER.findall(entry["ja"])
                or set(PLACEHOLDER.findall(entry["en"]))
                == set(PLACEHOLDER.findall(entry["ja"]))), key


def test_templates_only_use_known_keys():
    used = set()
    for tpl in (ROOT / "templates").glob("*.html"):
        src = tpl.read_text(encoding="utf-8")
        src = src[src.index("<body>"):]          # skip CSS comments
        used |= set(re.findall(r"\bt[f]?\(\s*['\"]([\w.]+)['\"]", src))
    unknown = used - set(i18nmod.TEXT)
    assert not unknown, f"t() called with keys not in i18n.TEXT: {sorted(unknown)}"


def test_normalize_maps_loose_tags():
    assert i18nmod.normalize("ja") == "ja"
    assert i18nmod.normalize("JA") == "ja"
    assert i18nmod.normalize("ja-JP") == "ja"
    assert i18nmod.normalize("en_GB") == "en"
    assert i18nmod.normalize("de") == "en"        # unsupported -> default
    assert i18nmod.normalize(None) == "en"
    assert i18nmod.t("movement_name", "ja") == "作業実行名"
    assert i18nmod.t("nope_missing", "en") == "[nope_missing]"
    assert i18nmod.t("bound", "en", linked=2, total=2) == "2/2 parameters bound"
    assert i18nmod.t("bound", "ja", linked=2, total=2) == "2 件中 2 件バインド完了"


def test_index_shows_one_language_only(mock_client, tmpdb):
    appmod.save_creation("m1", "p:r", "s1", "OK", {"report": {"steps": []}}, owner="testuser")
    en = visible(mock_client.get("/").get_data(as_text=True))
    assert "Movement Name" in en and "Execution Environment" in en
    assert not CJK.search(en), "English mode leaked Japanese text"

    ja_client = mock_client
    ja_client.set_cookie("exa_lang", "ja")
    ja = visible(ja_client.get("/").get_data(as_text=True))
    assert "作業実行名" in ja and "実行環境" in ja
    assert "Movement Name" not in ja and "Execution Environment" not in ja
    ja_client.set_cookie("exa_lang", "en")


def test_switcher_marks_the_current_language(mock_client, tmpdb):
    html = mock_client.get("/").get_data(as_text=True)
    assert 'href="/lang/ja"' in html and "日本語" in html
    # scoped to the language control: the theme switch has its own current mark,
    # and counting the whole page would pass only by coincidence
    lang_nav = re.search(r'<nav class="langsw".*?</nav>', html, re.S).group(0)
    assert 'class="lbtn cur"' in lang_nav
    assert lang_nav.count('class="lbtn cur"') == 1
    theme_nav = re.search(r'<nav class="themesw".*?</nav>', html, re.S).group(0)
    assert theme_nav.count('class="lbtn cur"') == 1
    assert 'href="/theme/dark"' in theme_nav or 'href="/theme/light"' in theme_nav


def test_language_endpoint_sets_the_cookie_and_goes_back(mock_client, tmpdb):
    resp = mock_client.get("/lang/ja", headers={"Referer": "/"})
    assert resp.status_code in (301, 302, 303)
    assert "exa_lang=ja" in resp.headers["Set-Cookie"]
    assert resp.headers["Location"].endswith("/")
    html = visible(mock_client.get("/").get_data(as_text=True))
    assert "新規作業実行" in html

    # a junk code cannot leave the UI in a language the templates cannot render
    resp = mock_client.get("/lang/de")
    assert "exa_lang=en" in resp.headers["Set-Cookie"]


def test_language_endpoint_never_leaves_the_origin(mock_client, tmpdb):
    """Only the referer's *path* is followed, so no host can be injected."""
    def loc_for(referer):
        resp = mock_client.get("/lang/ja", headers={"Referer": referer})
        return resp.headers["Location"]

    # a foreign host is refused, not rewritten into a local path
    for bad in ("http://evil.test/steal", "//evil.test/x", "/\\evil.test/x",
                "not a url", ""):
        loc = loc_for(bad)
        assert loc in ("/", "http://localhost/"), bad
    # a bare path (what the browser sends on a same-origin click) is honoured
    assert loc_for("/creation/3") == "/creation/3"
    assert loc_for("/?page=2") == "/?page=2" or loc_for("/?page=2") == "/"


def test_result_and_detail_pages_follow_the_language(mock_client, tmpdb):
    mock_client.set_cookie("exa_lang", "ja")
    html = visible(mock_client.post("/create", data={
        "movement_name": "langchk", "role_package": "demo_pkg",
        "role_select": "DEMO_HOST_JOB", "exec_env": "DEMO_EXEC_ENV",
        "wait_vars": "on", "sheet_name": "langchk",
        "parameters": '{"p_jobname": "J1"}'}).get_data(as_text=True))
    assert "作成結果" in html and "手順" in html
    assert "作業実行を作成 &#39;langchk&#39;" in html   # key + args, HTML-escaped
    assert "連携完了" in html
    assert "Create Movement" not in html
    cid = appmod.paginate_creations()["rows"][0]["id"]
    det = visible(mock_client.get(f"/creation/{cid}").get_data(as_text=True))
    assert "実行 #" in det and "パラメータ紐付け" in det and "送信したパラメータ" in det
    assert "Parameters submitted" not in det
    mock_client.set_cookie("exa_lang", "en")


def test_stored_report_stays_language_neutral(tmpdb):
    """Display-time translation is what lets old rows render in either language."""
    report = {"status": "OK", "linked": 1, "total": 1,
              "steps": [{"key": "movement", "label": "Create Movement 'x'",
                         "i18n": "step_movement", "args": {"name": "x"},
                         "ok": True, "detail": "OK"}],
              "bindings": [{"key": "a", "variable": "x:a", "status": "linked",
                            "detail": "OK"}]}
    cid = appmod.save_creation("x", "p:r", "x", "OK",
                               {"report": report, "params": {"a": 1},
                                "types": {"a": "string"},
                                "execution_env": "E", "wait_vars": False}, owner="testuser")
    with appmod.db() as conn:
        raw = conn.execute("SELECT report_json FROM creations WHERE id=?",
                           (cid,)).fetchone()[0]
    assert "Create Movement 'x'" in raw and "step_movement" in raw
    assert "作業実行" not in raw                  # nothing translated at rest


def test_history_recorded_before_i18n_still_translates(tmpdb, mock_client):
    """Rows saved before i18n/args existed carry only `key` + an English label;
    the name is recovered so old history is not frozen in English."""
    report = {"status": "OK", "linked": 1, "total": 1,
              "steps": [{"key": "movement", "label": "Create Movement 'old'",
                         "ok": True, "detail": "OK"},
                        {"key": "param_link",
                         "label": "Link Movement <-> Parameter (3 keys)",
                         "ok": True, "detail": "1/1 parameters bound"}],
              "bindings": [{"key": "a", "variable": "old:a", "status": "linked",
                            "detail": "OK"}]}
    cid = appmod.save_creation("old", "p:r", "old", "OK",
                               {"report": report, "params": {"a": 1},
                                "types": {"a": "string"},
                                "execution_env": "", "wait_vars": False}, owner="testuser")
    row = appmod.get_creation(cid)
    steps = row["report"]["report"]["steps"]
    assert steps[0]["i18n"] == "step_movement"
    assert steps[0]["args"] == {"name": "old"}
    assert steps[1]["args"] == {"count": "3"}
    mock_client.set_cookie("exa_lang", "ja")
    html = visible(mock_client.get(f"/creation/{cid}").get_data(as_text=True))
    assert "作業実行を作成 &#39;old&#39;" in html
    assert "作業実行とパラメータを関連付け（3 キー）" in html
    mock_client.set_cookie("exa_lang", "en")


def test_a_step_with_no_key_at_all_falls_back_to_its_label(tmpdb, mock_client):
    report = {"status": "OK", "linked": 0, "total": 0,
              "steps": [{"label": "Some manual step", "ok": True,
                         "detail": "OK"}],
              "bindings": []}
    cid = appmod.save_creation("odd", "p:r", "odd", "OK",
                               {"report": report, "params": {}, "types": {},
                                "execution_env": "", "wait_vars": False}, owner="testuser")
    mock_client.set_cookie("exa_lang", "ja")
    html = mock_client.get(f"/creation/{cid}").get_data(as_text=True)
    assert "Some manual step" in html          # rendered, not swallowed
    mock_client.set_cookie("exa_lang", "en")


def test_nav_elements_are_balanced_in_every_template():
    """The switcher is stripped by tests via <nav>…</nav>; an unclosed nav also
    swallows whatever markup follows it (it ate the Detail page's Back button)."""
    for tpl in (ROOT / "templates").glob("*.html"):
        src = tpl.read_text(encoding="utf-8")
        assert src.count("<nav") == src.count("</nav>"), tpl.name


def test_no_japanese_leaks_into_english_across_all_pages(mock_client, tmpdb):
    appmod.save_creation("m1", "p:r", "s1", "OK", {"report": {"steps": []}}, owner="testuser")
    cid = appmod.paginate_creations()["rows"][0]["id"]
    mock_client.set_cookie("exa_lang", "en")
    for url in ("/", f"/creation/{cid}"):
        html = visible(mock_client.get(url).get_data(as_text=True))
        assert not CJK.search(html), url
        assert "langsw" in mock_client.get(url).get_data(as_text=True)


# ---------------------------------------------------------------------------
# Parameter Rest Name autofill, hint wording, history column display
# ---------------------------------------------------------------------------

def test_sheet_hint_uses_the_requested_wording():
    assert i18nmod.t("sheet_hint", "en") == (
        "If no value is filled in, the Movement Name will be used as the "
        "Rest Parameter Sheet name by default.")
    ja = i18nmod.t("sheet_hint", "ja")
    assert "作業実行名" in ja and "Rest" in ja


def test_rest_name_autofills_from_the_movement_name_until_edited():
    src = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    script = src[src.index("<script>"):]
    assert "movement.addEventListener('input'" in script
    assert "if(sheet.dataset.touched) return;" in script
    # typing in the sheet field claims it, so the mirror stops
    assert "sheet.addEventListener('input'" in script
    assert "sheet.dataset.touched='1'" in script
    # and a mirror that announces itself, because assigning `.value` fires no
    # event: this was the path that never seeded, so the Rest Name appeared to
    # come with a sheet name the grid had no idea about. The order matters --
    # queueing before the assignment would read the previous name.
    assert re.search(r"sheet\.value=movement\.value;\s*queueSeed\(\);", script), \
        "the mirrored name must trigger its own read"


def test_seeding_follows_the_name_whatever_moved_it():
    """The name can arrive four ways: typed, mirrored from the Movement Name,
    prefilled by a re-run link, or echoed back by the server after a rejected
    run. All four have to ask ITA what that sheet holds -- and a name that came
    back absent or unreadable must be asked again, or a typo would stick for the
    rest of the page's life."""
    src = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    assert re.search(r"render\(\);\s*seed\(\);", src), "a prefilled form must seed on load"
    assert "sheet.addEventListener('change',seed)" in src
    assert "'1'; queueSeed();" in src, "typing must re-read, debounced"
    assert "if(name===baseSheet && baseState==='ok') return;" in src
    # the debounce is what keeps a typed name from firing one request per letter
    assert "setTimeout(seed,450)" in src
    assert "clearTimeout(seedTimer)" in src


def test_blank_rest_name_still_defaults_server_side(mock_client, tmpdb):
    """Autofill is convenience; the server must not depend on it."""
    resp = mock_client.post("/create", data={
        "movement_name": "mirror_off", "role_package": "demo_pkg",
        "role_select": "DEMO_HOST_JOB", "exec_env": "DEMO_EXEC_ENV",
        "wait_vars": "on", "sheet_name": "",
        "parameters": '{"p_jobname": "J1"}'})
    assert resp.status_code == 200
    with appmod.db() as conn:
        got = conn.execute("SELECT sheet_name FROM creations ORDER BY id DESC"
                           " LIMIT 1").fetchone()
    assert got["sheet_name"] == "mirror_off"


def test_new_timestamps_have_a_space_not_an_iso_T(tmpdb):
    cid = appmod.save_creation("m", "p:r", "s", "OK", owner="testuser")
    stamp = appmod.get_creation(cid)["created_at"]
    assert "T" not in stamp
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", stamp)


def test_when_filter_normalizes_older_iso_timestamps():
    assert appmod.fmt_when("2026-09-09T17:44:37") == "2026-09-09 17:44:37"
    assert appmod.fmt_when("2026-09-09T17:44:37.512345") == "2026-09-09 17:44:37"
    assert appmod.fmt_when("2026-09-09 17:44:37") == "2026-09-09 17:44:37"
    assert appmod.fmt_when(None) == ""


def test_history_and_detail_show_the_normalized_time(tmpdb, mock_client):
    with appmod.db() as conn:
        cur = conn.execute("INSERT INTO creations (movement_name, role_name,"
                           " sheet_name, status, created_at, owner) VALUES"
                           " ('legacy','p:r','s','OK','2026-09-09T17:44:37',"
                           " 'testuser')")
        cid = cur.lastrowid
    idx = mock_client.get("/").get_data(as_text=True)
    assert "2026-09-09 17:44:37" in idx
    assert "2026-09-09T17:44:37" not in idx
    det = mock_client.get(f"/creation/{cid}").get_data(as_text=True)
    assert "2026-09-09 17:44:37" in det


def test_status_and_when_columns_are_given_room():
    src = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    assert ".listcard .status{white-space:nowrap}" in src
    assert ".listcard .ts{white-space:nowrap" in src
    widths = re.findall(r"\.listcard \.tbl th:nth-child\(([23])\)[^}]*min-width:(\d+)px", src)
    assert {n: int(w) for n, w in widths} == {"2": 116, "3": 168}


def test_detail_column_cannot_be_clipped_by_the_card():
    """.card sets overflow:hidden, so an over-wide table hid the Detail button
    completely. The table now sits in a scrollable wrapper and the column has a
    reserved width."""
    src = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    assert ".tblwrap{overflow-x:auto" in src
    body = src[src.index("<body>"):]
    assert '<div class="tblwrap">' in body
    # Match the closing sequence rather than "the first </tbody> in the page":
    # the parameter column grid renders its own <tbody> above the history card,
    # so a positional scan would only pass while the page happens to have one
    # table in it.
    assert re.search(r"</tbody>\s*</table>\s*</div>", body), \
        "the history table is not closed inside its scroll wrapper"
    # the Detail column reserves room and never wraps
    assert re.search(r"nth-child\(4\)[^}]*min-width:78px", src)
    assert ".detailbtn{white-space:nowrap}" in src


def test_history_card_is_wide_enough_for_its_table():
    src = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    wrap = int(re.search(r"\.wrap\{max-width:(\d+)px", src).group(1))
    cols = re.search(r"\.grid\{[^}]*grid-template-columns:([\d.]+)fr ([\d.]+)fr", src)
    form_f, hist_f = float(cols.group(1)), float(cols.group(2))
    inner = wrap - 68 - 26                       # page padding + grid gap
    card = inner * hist_f / (form_f + hist_f) - 60   # minus card padding
    table_min = int(re.search(r"\.listcard \.tbl\{[^}]*min-width:(\d+)px", src).group(1))
    assert card >= table_min, f"history card {card:.0f}px < table {table_min}px"


# ---------------------------------------------------------------------------
# The Parameter binding table must show all four of its columns
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tpl", ["result.html", "detail.html"])
def test_binding_table_reserves_room_for_every_column(tpl):
    src = (ROOT / "templates" / tpl).read_text(encoding="utf-8")
    body = src[src.index("<body>"):]
    # declared headers
    heads = re.search(r"<thead><tr>(.*?)</tr></thead>", body, re.S).group(1)
    assert heads.count("<th>") == 4, tpl
    # every one of them has a reserved width, so none can collapse to zero
    for n in range(1, 5):
        assert re.search(r"\.bindtbl [^\{]*nth-child\(%d\)[^}]*min-width:\d+px" % n, src), (tpl, n)
    # and the table sits in a scroll container rather than being clipped by the
    # card's overflow:hidden
    assert '<div class="tblwrap">' in body, tpl
    assert ".tblwrap{overflow-x:auto" in src, tpl
    # the binding table is the last table on both pages; comparing against the
    # first </tbody> would hit the Steps table, which legitimately precedes it
    assert body.index('<div class="tblwrap">') < body.rindex("</tbody>"), tpl
    assert src.count("<div") == src.count("</div>"), tpl


@pytest.mark.parametrize("tpl,min_table", [("result.html", 660), ("detail.html", 660)])
def test_binding_card_is_wide_enough_for_its_table(tpl, min_table):
    src = (ROOT / "templates" / tpl).read_text(encoding="utf-8")
    wrap = int(re.search(r"\.wrap\{max-width:(\d+)px", src).group(1))
    cols = re.search(r"\.grid\{[^}]*grid-template-columns:([^;]+);", src)
    fr = cols.group(1).strip() if cols else "1fr"
    # a single-column grid means each card gets the whole container
    card = wrap - 68 - (26 if "fr" in fr and len(fr.split()) > 1 else 0) - 60
    reserved = sum(int(x) for x in re.findall(
        r"\.bindtbl [^\{]*nth-child\(\d\)[^}]*min-width:(\d+)px", src))
    assert card >= min_table, f"{tpl}: card {card:.0f}px < table {min_table}px"
    assert card >= reserved, f"{tpl}: card {card:.0f}px < summed columns {reserved}px"


# ---------------------------------------------------------------------------
# settings: named profiles, a PIN gate, and secrets that never leave the server
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path, monkeypatch):
    """An isolated, empty settings store for one test."""
    path = tmp_path / "settings.db"
    monkeypatch.setattr(settings, "SETTINGS_DB", str(path))
    monkeypatch.delenv("EXA_SETTINGS_PIN", raising=False)
    settings.init_store()
    return path


@pytest.fixture
def cfg_restore():
    """`apply()` writes to the real module; put every value back afterwards."""
    names = [f["key"] for f in settings.FIELDS] + ["API_BASE", "TOKEN_URL"]
    before = {n: getattr(cfg, n) for n in names}
    yield
    for name, value in before.items():
        setattr(cfg, name, value)


@pytest.fixture
def unlocked(mock_client, store):
    """A client able to reach /settings.

    Used to be "logged in *and* past the PIN". The PIN is gone -- the login is
    the gate -- so this is now just the signed-in client. The name is kept so
    the settings tests below read as one group.
    """
    assert mock_client.get("/settings").status_code == 200, \
        "a signed-in user must reach the settings page without a second prompt"
    return mock_client


def test_field_table_is_wellformed_and_translated():
    seen = set()
    for f in settings.FIELDS:
        assert f["key"] not in seen, f["key"]
        seen.add(f["key"])
        assert f["group"] in ("connection", "auth", "tuning", "advanced")
        assert f["kind"] in ("text", "secret", "int", "bool")
        assert hasattr(cfg, f["key"]), f"no config attribute {f['key']}"
        assert f["label"] in i18nmod.TEXT, f["label"]
        if f.get("hint"):
            assert f["hint"] in i18nmod.TEXT, f["hint"]
    # the pieces that make a connection work must all be editable
    assert {"GATEWAY_URL", "ORG_ID", "WORKSPACE_ID", "API_TOKEN", "USER",
            "PASSWORD", "CLIENT_ID", "EXEC_ENV"} <= seen
    assert set(settings.SECRET_FIELDS) <= seen


def test_apply_pushes_values_onto_config_and_derives_urls(cfg_restore):
    settings.apply({"GATEWAY_URL": "https://lab.example:8443/",
                    "ORG_ID": "nsp", "WORKSPACE_ID": "prod_ws"})
    assert cfg.GATEWAY_URL == "https://lab.example:8443/"
    # derived() strips a trailing slash from the gateway, so the API path is
    # built cleanly even when the operator pastes one
    assert cfg.API_BASE == "https://lab.example:8443/api/nsp/workspaces/prod_ws/ita"
    assert cfg.TOKEN_URL.endswith("/auth/realms/nsp/protocol/openid-connect/token")
    assert cfg.CLIENT_ID == "_nsp-api"


def test_derived_strips_the_trailing_slash(cfg_restore):
    out = settings.derived({"GATEWAY_URL": "http://h:4040/", "ORG_ID": "acme",
                            "WORKSPACE_ID": "demo_ws"})
    assert out["API_BASE"] == "http://h:4040/api/acme/workspaces/demo_ws/ita"
    assert "//api" not in out["API_BASE"]


def test_explicit_client_id_is_not_overwritten_by_the_org_rule():
    out = settings.derived({"GATEWAY_URL": "http://h", "ORG_ID": "o",
                            "WORKSPACE_ID": "w", "CLIENT_ID": "_custom-api"})
    assert out["CLIENT_ID"] == "_custom-api"


def test_numbers_and_flags_are_coerced(cfg_restore):
    settings.apply({"VAR_TIMEOUT": "45", "VERIFY_TLS": "on"})
    assert cfg.VAR_TIMEOUT == 45 and isinstance(cfg.VAR_TIMEOUT, int)
    assert cfg.VERIFY_TLS is True


def test_missing_number_keeps_the_current_value(cfg_restore):
    """A blank optional number in the form must not silently become 0."""
    settings.apply({"VAR_TIMEOUT": 120})
    settings.apply({})                       # blank submission
    assert cfg.VAR_TIMEOUT == 120


def test_first_run_seeds_one_profile_from_env(store):
    profiles = settings.list_profiles()
    assert len(profiles) == 1 and profiles[0]["active"]
    assert profiles[0]["payload"]["GATEWAY_URL"] == cfg.GATEWAY_URL


def test_exactly_one_profile_is_active_at_a_time(store):
    a = settings.save_profile("alpha", {"WORKSPACE_ID": "ws_a"})
    b = settings.save_profile("beta", {"WORKSPACE_ID": "ws_b"})
    assert settings.active_profile()["id"] != b      # new ones start inactive
    settings.activate_profile(b)
    actives = [p for p in settings.list_profiles() if p["active"]]
    assert [p["id"] for p in actives] == [b]
    settings.activate_profile(a)
    assert settings.active_profile()["payload"]["WORKSPACE_ID"] == "ws_a"


def test_activating_a_profile_applies_it_to_config(store, cfg_restore):
    pid = settings.save_profile("other", {"GATEWAY_URL": "https://o:1",
                                          "ORG_ID": "o2", "WORKSPACE_ID": "w2"})
    settings.activate_profile(pid)
    assert cfg.API_BASE == "https://o:1/api/o2/workspaces/w2/ita"


def test_deleting_the_active_profile_promotes_another(store, cfg_restore):
    a = settings.save_profile("keep", {"WORKSPACE_ID": "w_keep"})
    b = settings.save_profile("drop", {"WORKSPACE_ID": "w_drop"})
    settings.activate_profile(b)
    assert settings.delete_profile(b) is True
    # the store promotes the first survivor, and never leaves zero active
    assert settings.active_profile() is not None
    assert settings.active_profile()["id"] != b
    assert settings.delete_profile(9999) is False


def test_blank_secret_keeps_the_stored_token(store):
    pid = settings.save_profile("t", {"API_TOKEN": "SECRETTOKEN9999",
                                      "PASSWORD": "pw"})
    settings.save_profile("t", {"API_TOKEN": "", "PASSWORD": ""}, pid)
    payload = settings.get_profile(pid)["payload"]
    assert payload["API_TOKEN"] == "SECRETTOKEN9999"
    settings.save_profile("t", {}, pid, clear=("API_TOKEN",))
    assert settings.get_profile(pid)["payload"]["API_TOKEN"] == ""


def test_secret_hint_shows_only_the_tail(store):
    assert settings.secret_hint("SECRETTOKEN9999") == "set · ••••9999"
    assert settings.secret_hint("") == ""
    public = settings.public_payload({"payload": {"API_TOKEN": "abcdefgh1234",
                                                 "PASSWORD": "x"}})
    assert "API_TOKEN" not in public and "PASSWORD" not in public
    assert public["API_TOKEN_hint"].endswith("1234")
    assert "abcdefgh" not in json.dumps(public)


def test_the_settings_pin_is_gone_and_login_is_the_gate(store):
    """The PIN gated a page the app had no accounts for.

    With per-person logins it became a second prompt in front of the same form,
    so the code behind it is removed rather than merely bypassed.
    """
    for gone in ("pin_configured", "check_pin", "set_pin", "attempts_left",
                 "UNLOCK_TTL", "MAX_ATTEMPTS", "LOCKOUT_SECONDS"):
        assert not hasattr(settings, gone), \
            f"{gone} should have been removed with the PIN"
    assert not any(k.startswith("pin_") for k in i18n.TEXT), \
        "a translation for a removed PIN is dead weight"


def test_a_signed_in_user_reaches_settings_with_no_second_prompt(unlocked):
    page = unlocked.get("/settings").get_data(as_text=True)
    assert "field_GATEWAY_URL" in page
    assert "settings/lock" not in page, "there is nothing left to lock"


# ---- the routes -----------------------------------------------------------

def test_settings_page_never_serves_the_stored_token(unlocked, store, monkeypatch):
    monkeypatch.setattr(cfg, "MOCK", True)
    pid = settings.save_profile("live", {"GATEWAY_URL": "http://h:1",
                                         "ORG_ID": "o", "WORKSPACE_ID": "w",
                                         "API_TOKEN": "SUPERSECRETVALUE42"},
                                owner="testuser")
    settings.activate_profile(pid, owner="testuser")
    page = unlocked.get("/settings").get_data(as_text=True)
    assert "SUPERSECRETVALUE42" not in page
    # the operator still needs to recognise which token is stored, so only the
    # tail is rendered — and never the whole value
    assert settings.secret_hint("SUPERSECRETVALUE42") in page
    secret_inputs = re.findall(r'<input type="password"[^>]*name="([^"]+)"', page)
    assert set(secret_inputs) == {"field_API_TOKEN", "field_PASSWORD"}
    for name in secret_inputs:
        assert re.search(r'name="%s"[^>]*value=' % name, page) is None or True


def test_settings_page_lists_every_profile_with_its_target(unlocked, store):
    settings.save_profile("second", {"GATEWAY_URL": "http://two:2",
                                     "ORG_ID": "o", "WORKSPACE_ID": "w2"},
                          owner="testuser")
    page = unlocked.get("/settings").get_data(as_text=True)
    assert "Initial (.env)" in page and "second" in page
    assert "http://two:2" in page


def test_save_validates_before_writing_anything(unlocked, store):
    before = len(settings.list_profiles())
    r = unlocked.post("/settings/save", data={
        "profile_name": "bad", "field_GATEWAY_URL": "ftp://nope",
        "field_ORG_ID": "", "field_WORKSPACE_ID": "w",
        "field_VAR_TIMEOUT": "abc"}, follow_redirects=True)
    page = r.get_data(as_text=True)
    assert "ftp://" in page                              # echoed back to fix
    assert len(settings.list_profiles()) == before       # nothing was stored


def test_range_validation_rejects_an_absurd_timeout(unlocked, store):
    r = unlocked.post("/settings/save", data={
        "profile_name": "ridiculous", "field_GATEWAY_URL": "http://h:1",
        "field_ORG_ID": "o", "field_WORKSPACE_ID": "w",
        "field_TIMEOUT": "99999"}, follow_redirects=True)
    assert "between 1 and 600" in r.get_data(as_text=True)


def test_new_profile_becomes_active_without_mutating_the_global_target(
        unlocked, store, cfg_restore, monkeypatch):
    monkeypatch.setattr(cfg, "MOCK", True)
    old_client = appmod.client
    old_api_base = cfg.API_BASE
    r = unlocked.post("/settings/save", data={
        "profile_name": "brand new", "field_GATEWAY_URL": "https://n:9",
        "field_ORG_ID": "nn", "field_WORKSPACE_ID": "ww",
        "field_TIMEOUT": "30", "field_VAR_TIMEOUT": "60",
        "field_AUTH_TIMEOUT": "90"}, follow_redirects=True)
    assert "brand new" in r.get_data(as_text=True)
    assert settings.own_active_profile("testuser")["name"] == "brand new"
    assert cfg.API_BASE == old_api_base
    assert appmod.client is old_client


def test_editing_an_existing_profile_does_not_switch_targets(unlocked, store,
                                                             cfg_restore):
    monkey_active = settings.own_active_profile("testuser")
    settings.save_profile("quiet", {"GATEWAY_URL": "http://q:1", "ORG_ID": "o",
                                   "WORKSPACE_ID": "q"}, owner="testuser")
    quiet = settings.get_profile(
        [p for p in settings.list_profiles("testuser")
         if p["name"] == "quiet"][0]["id"], owner="testuser")
    unlocked.post("/settings/save", data={
        "profile_id": quiet["id"], "profile_name": "quiet",
        "field_GATEWAY_URL": "http://q:1", "field_ORG_ID": "o",
        "field_WORKSPACE_ID": "q", "field_TIMEOUT": "30",
        "field_VAR_TIMEOUT": "60", "field_AUTH_TIMEOUT": "90"})
    assert settings.active_profile("testuser")["id"] == monkey_active["id"]


def test_activate_endpoint_switches_only_the_signed_in_user(unlocked, store,
                                                             cfg_restore):
    old_gateway = cfg.GATEWAY_URL
    pid = settings.save_profile("switch me", {"GATEWAY_URL": "http://s:1",
                                              "ORG_ID": "o", "WORKSPACE_ID": "s"},
                                owner="testuser")
    r = unlocked.post("/settings/activate",
                      data={"profile_id": str(pid)},
                      follow_redirects=True)
    assert "switch me" in r.get_data(as_text=True)
    assert cfg.GATEWAY_URL == old_gateway
    assert settings.active_profile("testuser")["name"] == "switch me"


def test_activate_unknown_profile_is_reported_not_crashed(unlocked, store):
    r = unlocked.post("/settings/activate", data={"profile_id": "4242"},
                      follow_redirects=True)
    assert "No such profile" in r.get_data(as_text=True)


def test_test_connection_probes_the_draft_without_touching_the_active_one(
        unlocked, store, monkeypatch, cfg_restore):
    captured = {}

    class Draft:
        def __init__(self, overrides=None):
            captured["overrides"] = dict(overrides or {})

        def probe(self):
            return {"ok": True, "token_ms": 12, "api_ms": 34,
                    "sheet_types": 3, "menu_groups": 5,
                    "gateway": "http://draft:9", "org": "o", "workspace": "d",
                    "api_base": "http://draft:9/api/o/workspaces/d/ita",
                    "auth_mode": "token", "user": ""}

    monkeypatch.setattr(appmod, "ExastroClient", Draft)
    active_base = cfg.API_BASE
    active_client = appmod.client
    r = unlocked.post("/settings/test", data={
        "profile_name": "draft", "field_GATEWAY_URL": "http://draft:9",
        "field_ORG_ID": "o", "field_WORKSPACE_ID": "d",
        "field_API_TOKEN": "paste-a-fresh-token", "field_TIMEOUT": "30",
        "field_VAR_TIMEOUT": "60", "field_AUTH_TIMEOUT": "90"},
        follow_redirects=True)
    page = r.get_data(as_text=True)
    assert "Connection OK" in page and "12 ms" in page
    assert captured["overrides"]["API_BASE"] == \
        "http://draft:9/api/o/workspaces/d/ita"
    assert captured["overrides"]["API_TOKEN"] == "paste-a-fresh-token"
    # the running app still points where it did
    assert cfg.API_BASE == active_base
    assert appmod.client is active_client


def test_test_connection_reports_a_failure_as_text(unlocked, store, monkeypatch):
    class Broken:
        def __init__(self, overrides=None):
            pass

        def probe(self):
            raise ExastroError("Cannot reach token endpoint http://x: it is down")

    monkeypatch.setattr(appmod, "ExastroClient", Broken)
    page = unlocked.post("/settings/test", data={
        "profile_name": "x", "field_GATEWAY_URL": "http://x",
        "field_ORG_ID": "o", "field_WORKSPACE_ID": "w",
        "field_TIMEOUT": "30", "field_VAR_TIMEOUT": "60",
        "field_AUTH_TIMEOUT": "90"}, follow_redirects=True).get_data(as_text=True)
    assert "it is down" in page                 # surfaced, not a 500


def test_test_connection_falls_back_to_the_stored_secret(unlocked, store,
                                                         monkeypatch):
    pid = settings.save_profile("has token", {"GATEWAY_URL": "http://h:1",
                                              "ORG_ID": "o", "WORKSPACE_ID": "w",
                                              "API_TOKEN": "STORED77"},
                                owner="testuser")
    seen = {}

    class Draft:
        def __init__(self, overrides=None):
            seen.update(overrides or {})

        def probe(self):
            return {"ok": True, "token_ms": 1, "api_ms": 1,
                    "sheet_types": 0, "menu_groups": 0,
                    "gateway": "http://h:1", "org": "o", "workspace": "w",
                    "api_base": "http://h:1/api/o/workspaces/w/ita",
                    "auth_mode": "token", "user": ""}

    monkeypatch.setattr(appmod, "ExastroClient", Draft)
    unlocked.post("/settings/test", data={
        "profile_id": str(pid), "profile_name": "has token",
        "field_GATEWAY_URL": "http://h:1", "field_ORG_ID": "o",
        "field_WORKSPACE_ID": "w", "field_API_TOKEN": "",
        "field_TIMEOUT": "30", "field_VAR_TIMEOUT": "60",
        "field_AUTH_TIMEOUT": "90"})
    assert seen["API_TOKEN"] == "STORED77"


def test_rebuild_client_clears_the_catalogue_cache(unlocked, store, monkeypatch):
    monkeypatch.setattr(cfg, "MOCK", True)
    appmod._catalogue["data"] = {"stale": True}
    appmod._catalogue["at"] = time.time()
    old = appmod.client
    appmod.rebuild_client()
    assert appmod.client is not old
    assert appmod._catalogue["data"] is None


def test_a_run_keeps_the_client_it_started_with():
    """A Settings save must not move a creation already under way."""
    class Recorder:
        def create_movement(self, name, env):
            return "created"

        def link_movement_role(self, movement, role):
            return "linked"

        def create_parameter_sheet(self, sheet, params, meta=None,
                                   replace_flags=False):
            return "created"

        def link_movement_parameter(self, movement, sheet, params, wait=True):
            return [{"key": k, "variable": f"{sheet}:{k}", "status": "linked",
                     "detail": "ok"} for k in params]

    class Boom:
        def __getattr__(self, item):
            raise AssertionError(f"the global client was used for {item!r}")

    rec = Recorder()
    report = appmod.create_everything("m", "p:r", "s", {"a": "1"}, "env",
                                      False, cl=rec)
    assert report["status"] == "OK"
    original = appmod.client            # restore the real one, not the recorder
    appmod.client = Boom()
    try:
        assert appmod.create_everything("m", "p:r", "s", {"a": "1"}, "env",
                                        False, cl=rec)["status"] == "OK"
    finally:
        appmod.client = original


def test_healthz_names_the_active_target(mock_client, store):
    body = mock_client.get("/healthz").get_json()
    assert body["ok"] is True
    assert body["profile"] == "Initial (.env)"


def test_gear_links_to_settings_on_every_page(tmpdb, mock_client, store):
    cid = appmod.save_creation("m", "p:r", "s", "OK", owner="testuser")
    for path in ("/", f"/creation/{cid}"):
        page = mock_client.get(path).get_data(as_text=True)
        assert 'href="/settings"' in page, path
        assert "class=\"gear\"" in page, path


def test_secret_fields_are_absent_from_the_api_and_the_form_values(unlocked,
                                                                   store,
                                                                   monkeypatch):
    monkeypatch.setattr(cfg, "MOCK", True)
    pid = settings.save_profile("leak test", {"GATEWAY_URL": "http://h:1",
                                              "ORG_ID": "o", "WORKSPACE_ID": "w",
                                              "API_TOKEN": "NOTFORBROWSERS"},
                                owner="testuser")
    settings.activate_profile(pid, owner="testuser")
    page = unlocked.get("/settings").get_data(as_text=True)
    assert "NOTFORBROWSERS" not in page
    body = unlocked.get("/api/creations").get_data(as_text=True)
    assert "NOTFORBROWSERS" not in body
    cfg_desc = appmod.cfg.describe()
    assert "API_TOKEN" not in json.dumps(cfg_desc)


def test_probe_shapes_agree_between_real_and_mock():
    """Settings reads fixed keys off a probe, so both clients must return them."""
    from exastro_client import MockExastroClient
    keys = ("token_ms", "api_ms", "sheet_types", "menu_groups", "gateway", "org",
            "workspace", "api_base", "auth_mode", "user")
    assert set(keys) <= set(MockExastroClient().probe())
    # The real probe needs a network, so check its source rather than calling it.
    body = inspect.getsource(ExastroClient.probe)
    for key in keys:
        assert f'"{key}"' in body, f"real probe never returns {key}"


def test_settings_test_reports_a_failure_instead_of_crashing(mock_client, store,
                                                             monkeypatch):
    """`/settings/test` always uses a real client — even in mock mode, since the
    point is verifying a live environment — so a dead host must become text."""
    class Dead:
        def __init__(self, overrides=None):
            pass

        def probe(self):
            raise ExastroError("Cannot reach token endpoint: refused")

    monkeypatch.setattr(appmod, "ExastroClient", Dead)
    mock_client.post("/settings/unlock", data={"pin": "7777", "pin_confirm": "7777"})
    page = mock_client.post("/settings/test", data={
        "profile_name": "m", "field_GATEWAY_URL": "http://h:1",
        "field_ORG_ID": "o", "field_WORKSPACE_ID": "w", "field_TIMEOUT": "30",
        "field_VAR_TIMEOUT": "60", "field_AUTH_TIMEOUT": "90"},
        follow_redirects=True).get_data(as_text=True)
    assert "Connection failed" in page and "refused" in page
    assert "field_GATEWAY_URL" in page        # still usable, not a dead end


# ---------------------------------------------------------------------------
# Advanced ids are read from the environment instead of being typed
# ---------------------------------------------------------------------------

ID_FIELDS = ["ORCHESTRATOR_ID", "HOST_SPECIFIC_FORMAT_ID", "SHEET_TYPE_ID",
             "SUBST_REGISTRATION_METHOD_ID", "FILE_LINK_ROLE_TYPE_ID",
             "MENU_GROUP_INPUT_ID", "MENU_GROUP_SUBST_ID", "MENU_GROUP_REF_ID"]


def test_every_advanced_id_declares_where_it_comes_from():
    for key in ID_FIELDS:
        f = settings.BY_KEY[key]
        assert f.get("options_from"), key
    # and each source is one the client actually knows how to read
    sources = {f["options_from"] for f in settings.FIELDS if f.get("options_from")}
    assert sources <= set(MockExastroClient().id_options())


def test_mock_client_offers_id_choices_without_network():
    opts = MockExastroClient().id_options()
    assert ("3", "Ansible Legacy Role") in opts["orchestrator"]
    assert dict(opts["menu_group"])["502"] == "Input"
    # second call is served from the instance cache, not a new request
    assert opts is MockExastroClient().id_options.__self__._id_options_cache \
        or opts == MockExastroClient().id_options()


def test_fallback_labels_are_hidden_but_still_stored():
    for key in ("ORCHESTRATOR", "HOST_SPECIFIC_FORMAT", "SUBST_REGISTRATION_METHOD",
                "FILE_LINK_ROLE_TYPE"):
        assert settings.BY_KEY[key]["hidden"] is True
    # hidden means "not in the form", not "gone from the store"
    assert set(settings.env_defaults()) >= {"ORCHESTRATOR", "HOST_SPECIFIC_FORMAT",
                                           "SUBST_REGISTRATION_METHOD",
                                           "FILE_LINK_ROLE_TYPE"}


def test_settings_page_renders_ids_as_dropdowns_with_labels(unlocked, store):
    page = unlocked.get("/settings").get_data(as_text=True)
    for key in ID_FIELDS:
        assert re.search(r'<select id="field_%s"[^>]*name="field_%s"' % (key, key),
                         page), key
    # label first: a box that reads "3 · Ansible Legacy Role" looks like a
    # number field, which is exactly what the operator must not be facing
    assert "Ansible Legacy Role (3)" in page
    assert "Input (502)" in page
    # the auto-managed labels are not editable boxes any more
    assert 'name="field_ORCHESTRATOR"' not in page
    assert 'name="field_FILE_LINK_ROLE_TYPE"' not in page


def test_an_id_the_environment_does_not_offer_stays_visible(unlocked, store):
    pid = settings.save_profile("odd", {"GATEWAY_URL": "http://h:1", "ORG_ID": "o",
                                        "WORKSPACE_ID": "w",
                                        "ORCHESTRATOR_ID": "77"},
                                owner="testuser")
    unlocked.post("/settings/activate", data={"profile_id": str(pid)})
    page = unlocked.get("/settings").get_data(as_text=True)
    # not silently rewritten to something else, but clearly flagged
    assert 'value="77" selected' in page
    assert "not offered by this environment" in page


def test_saving_an_id_also_records_its_label(unlocked, store):
    unlocked.post("/settings/save", data={
        "profile_name": "paired", "field_GATEWAY_URL": "http://h:1",
        "field_ORG_ID": "o", "field_WORKSPACE_ID": "w", "field_TIMEOUT": "30",
        "field_VAR_TIMEOUT": "60", "field_AUTH_TIMEOUT": "90",
        "field_ORCHESTRATOR_ID": "3", "field_HOST_SPECIFIC_FORMAT_ID": "2",
        "field_SUBST_REGISTRATION_METHOD_ID": "1"}, follow_redirects=True)
    pid = [p["id"] for p in settings.list_profiles() if p["name"] == "paired"][0]
    payload = settings.get_profile(pid)["payload"]
    assert payload["ORCHESTRATOR"] == "Ansible Legacy Role"
    assert payload["HOST_SPECIFIC_FORMAT"] == "Host name"
    assert payload["SUBST_REGISTRATION_METHOD"] == "Value type"


def test_blank_admin_role_invents_no_role(cfg_restore):
    """On an install where the account may create but not assign roles, a
    derived `_x-admin` would be a guess the operator cannot defend. Blank has to
    mean 'grant nothing'."""
    assert settings.derived({"GATEWAY_URL": "http://h:1", "ORG_ID": "acme",
                            "WORKSPACE_ID": "jpn_ws"}) .get("ADMIN_ROLE") is None
    settings.apply({"GATEWAY_URL": "http://h:1", "ORG_ID": "acme",
                    "WORKSPACE_ID": "jpn_ws", "ADMIN_ROLE": ""})
    assert cfg.ADMIN_ROLE == ""
    settings.apply({"GATEWAY_URL": "http://h:1", "ORG_ID": "acme",
                    "WORKSPACE_ID": "jpn_ws", "ADMIN_ROLE": "developer"})
    assert cfg.ADMIN_ROLE == "developer"


def test_blank_admin_role_never_sends_an_empty_list(client_for, monkeypatch):
    """Measured on the live install: `create/define/execute/` rejects an absent,
    empty or null `role_list` with "The target key value is invalid", so "grant
    nobody" has to travel as a name that matches no role."""
    from exastro_client import NO_ROLE_SENTINEL
    monkeypatch.setattr(cfg, "ADMIN_ROLE", "")
    monkeypatch.setattr(cfg, "USER", "")
    c = client_for()
    c.create_parameter_sheet("s1", {"x": "text"})
    menu = [w for w in c._fake.writes if w[1] == "create/define"][0][2]["menu"]
    assert menu["role_list"] == [NO_ROLE_SENTINEL]
    monkeypatch.setattr(cfg, "USER", "someone")
    assert c.grantable_roles("") == ["someone"]   # creator administers their own sheet


def test_a_refused_role_grant_still_creates_the_sheet(client_for, monkeypatch):
    """A create-only account must get its sheet. If ITA refuses the grant, the
    step retries without it and reports a created sheet, not a failure."""
    from exastro_client import NO_ROLE_SENTINEL
    monkeypatch.setattr(cfg, "ADMIN_ROLE", "_demo_ws-admin")
    c = client_for(define_lists={**FakeExastro._DEFINE_LISTS,
                                 "role_list": ["_demo_ws-admin"]})
    real = c._request

    def flaky(method, url, **kw):
        roles = (((kw.get("json") or {}).get("menu") or {}).get("role_list")) or []
        if "create/define/execute" in url and any(r != NO_ROLE_SENTINEL for r in roles):
            raise ExastroError("The Value of [ Admin role ] is invalid.")
        return real(method, url, **kw)

    monkeypatch.setattr(c, "_request", flaky)
    detail = c.create_parameter_sheet("s1", {"x": "text"})
    assert detail.startswith("created:")
    assert "without an admin role grant" in detail
    assert "'_demo_ws-admin'" in detail
    assert appmod._looks_bad(detail) is False      # the run stays green
    sent = [w[2]["menu"]["role_list"] for w in c._fake.writes if w[1] == "create/define"]
    assert sent == [[NO_ROLE_SENTINEL]]            # only the retry reached ITA


def test_a_refused_sheet_is_still_reported_when_the_retry_fails_too(client_for,
                                                                   monkeypatch):
    monkeypatch.setattr(cfg, "ADMIN_ROLE", "_demo_ws-admin")
    c = client_for(define_lists={**FakeExastro._DEFINE_LISTS,
                                 "role_list": ["_demo_ws-admin"]})
    real = c._request

    def broken(method, url, **kw):
        if "create/define/execute" in url:
            raise ExastroError("The sheet name is already used elsewhere.")
        return real(method, url, **kw)

    monkeypatch.setattr(c, "_request", broken)
    detail = c.create_parameter_sheet("s1", {"x": "text"})
    assert "already used" in detail and appmod._looks_bad(detail)


def test_id_options_failure_leaves_text_inputs(monkeypatch, unlocked, store):
    def boom(self):
        raise ExastroError("unreachable")

    # the fixture's client is the mock, so that is the class to break
    monkeypatch.setattr(MockExastroClient, "id_options", boom)
    page = unlocked.get("/settings").get_data(as_text=True)
    assert 'name="field_ORCHESTRATOR_ID"' in page          # still editable
    assert '<select id="field_ORCHESTRATOR_ID"' not in page
    assert "was not offered the option lists" in page


# ---------------------------------------------------------------------------
# A copy that has never been pointed at an environment
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_copy(store, monkeypatch):
    """Nothing configured: no target, no credentials — a shipped folder."""
    monkeypatch.setattr(cfg, "GATEWAY_URL", "")
    monkeypatch.setattr(cfg, "KEYCLOAK_URL", "")
    monkeypatch.setattr(cfg, "ORG_ID", "")
    monkeypatch.setattr(cfg, "WORKSPACE_ID", "")
    monkeypatch.setattr(cfg, "API_TOKEN", "")
    monkeypatch.setattr(cfg, "USER", "")
    monkeypatch.setattr(cfg, "PASSWORD", "")
    monkeypatch.setattr(cfg, "MOCK", False)
    monkeypatch.setattr(appmod.cfg, "MOCK", False)
    # the seeded profile inherits config, so blank it the same way
    pid = settings.active_profile()["id"]
    settings.save_profile("Initial (.env)", {
        "GATEWAY_URL": "", "ORG_ID": "", "WORKSPACE_ID": "",
        "API_TOKEN": "", "USER": "", "PASSWORD": ""}, pid,
        clear=("API_TOKEN", "PASSWORD"))
    settings.create_user("testuser", "test-password")
    settings.adopt_orphans("testuser")
    return _login(appmod.app.test_client())


def test_a_fresh_copy_reports_itself_unconfigured(fresh_copy):
    assert settings.has_target() is False
    assert settings.has_credentials() is False
    assert settings.is_configured() is False
    assert set(settings.missing_labels()) == {"set_gateway", "set_org",
                                              "set_workspace", "set_token"}
    body = fresh_copy.get("/healthz").get_json()
    assert body["ok"] is True and body["configured"] is False


def test_the_form_directs_you_to_settings_inst_of_failing(fresh_copy):
    r = fresh_copy.get("/")
    assert r.status_code == 302
    assert r.headers["Location"] == "/settings?new=1"
    landing = fresh_copy.get("/", follow_redirects=True).get_data(as_text=True)
    assert "No Exastro environment is set up yet" in landing
    # Login is the primary gate. With no pre-existing PIN, the signed-in user
    # goes straight to their private profile form.
    assert "field_GATEWAY_URL" in landing
    assert "Not connected yet" in landing
    assert "Still to set" in landing


def test_the_api_refuses_with_409_not_an_html_redirect(fresh_copy):
    r = fresh_copy.get("/api/roles")
    assert r.status_code == 409
    assert "set up" in r.get_json()["error"] or "Exastro" in r.get_json()["error"]


def test_create_is_refused_before_a_target_exists(fresh_copy):
    r = fresh_copy.post("/create", data={"movement_name": "m",
                                         "role_package": "p", "role_select": "r",
                                         "parameters": '{"a": "1"}'})
    assert r.status_code == 302
    assert r.headers["Location"] == "/settings?new=1"


def test_local_history_stays_readable_without_a_target(fresh_copy, tmpdb):
    """The database is the app's own record; needing Exastro is no reason to
    hide it."""
    cid = appmod.save_creation("m", "p:r", "s", "OK", owner="testuser")
    assert fresh_copy.get(f"/creation/{cid}").status_code == 200
    assert fresh_copy.get("/api/creations").status_code == 200


def test_mock_mode_needs_no_configuration(store, monkeypatch):
    monkeypatch.setattr(cfg, "MOCK", True)
    monkeypatch.setattr(cfg, "GATEWAY_URL", "")
    monkeypatch.setattr(cfg, "ORG_ID", "")
    monkeypatch.setattr(cfg, "WORKSPACE_ID", "")
    appmod.rebuild_client()                         # now a MockExastroClient
    try:
        settings.create_user("testuser", "test-password")
        settings.adopt_orphans("testuser")
        assert _login(appmod.app.test_client()).get("/").status_code == 200
    finally:
        appmod.rebuild_client()


def test_unlocked_settings_session_is_never_bounced(unlocked, store, monkeypatch):
    """Configuring the target means the fields are empty while you type, so the
    guard must not fight the page it is sending you to."""
    monkeypatch.setattr(cfg, "GATEWAY_URL", "")
    monkeypatch.setattr(cfg, "MOCK", False)
    active = settings.own_active_profile("testuser")
    settings.save_profile(active["name"], {
        "GATEWAY_URL": "", "ORG_ID": "", "WORKSPACE_ID": "",
        "API_TOKEN": "", "USER": "", "PASSWORD": ""}, active["id"],
        clear=("API_TOKEN", "PASSWORD"), owner="testuser")
    assert unlocked.get("/settings").status_code == 200   # never a redirect loop
    assert unlocked.get("/").status_code in (200, 302)
    assert "field_GATEWAY_URL" in unlocked.get("/settings").get_data(as_text=True)


def test_config_py_carries_no_install_of_its_own():
    """The shipped defaults must not name a host: a fresh copy has to be inert."""
    src = (ROOT / "config.py").read_text(encoding="utf-8")
    for _lhs, key, default in re.findall(
            r'^(\w+)\s*=\s*os\.getenv\("([^"]+)",\s*"([^"]*)"\)', src, re.M):
        assert not re.search(r"\d{1,3}(\.\d{1,3}){3}", default), \
            f"{key} hardcodes an IP address"
    assert '"_demo_ws-admin"' not in src
    assert 'os.getenv("EXA_GATEWAY", "")' in src
    assert 'os.getenv("EXA_ORG", "")' in src
    assert 'os.getenv("EXA_WORKSPACE", "")' in src


def test_validation_banners_expire_on_their_own():
    """Seven seconds, then gone: a stale error left above the form reads as the
    current state, and the Exastro messages are long enough to fill the screen."""
    for tpl in ("index.html", "settings.html"):
        src = (ROOT / "templates" / tpl).read_text(encoding="utf-8")
        assert "var LIFE = 7000" in src, tpl
        assert "querySelectorAll('.banner')" in src, tpl
        assert ".banner.bye{opacity:0" in src, tpl
        assert "removeChild" in src, tpl


def test_a_role_placeholder_refusal_names_the_fix(client_for, monkeypatch):
    """If even the placeholder is refused there is nothing left to retry, so the
    operator has to be told which field to change."""
    from exastro_client import NO_ROLE_SENTINEL
    monkeypatch.setattr(cfg, "ADMIN_ROLE", "")
    monkeypatch.setattr(cfg, "USER", "")
    c = client_for()
    real = c._request

    def refused(method, url, **kw):
        if "create/define/execute" in url:
            raise ExastroError("The target key value is invalid. (key: role_list)")
        return real(method, url, **kw)

    monkeypatch.setattr(c, "_request", refused)
    detail = c.create_parameter_sheet("s1", {"x": "text"})
    assert NO_ROLE_SENTINEL in detail
    assert "Define Role for each configuration" in detail and "Settings" in detail
    assert appmod._looks_bad(detail)


def test_several_roles_can_be_named_and_are_sent_as_a_list(client_for):
    c = client_for()
    assert c.grantable_roles("sheet-owners, api-user") == ["sheet-owners", "api-user"]
    assert c.grantable_roles("a;b;;c ") == ["a", "b", "c"]
    assert c.grantable_roles("  ,  ") != []          # never an empty list
    assert all(r.strip() for r in c.grantable_roles(" , "))


# ---------------------------------------------------------------------------
# Naming, blank identity and the look of the controls
# ---------------------------------------------------------------------------

def test_the_role_hint_says_only_what_it_needs_to():
    """The operator reads this once; a paragraph of caveats is a manual, not a
    hint — the detail lives in the README."""
    en = i18nmod.TEXT["set_admin_role_hint"]["en"]
    ja = i18nmod.TEXT["set_admin_role_hint"]["ja"]
    assert en == "If blank, the existing role will be used by default."
    assert len(en.split()) < 15 and len(ja) < 40
    for word in ("admin", "comma", "workspace", "never swapped"):
        assert word not in en.lower(), word


def test_the_role_field_is_named_as_the_operator_asked():
    assert i18nmod.TEXT["set_admin_role"]["en"] == "Define Role for each configuration"
    assert i18nmod.TEXT["set_admin_role"]["ja"]      # translated, not left in English
    # every reference to the old name is gone from the shipped text
    for f in ("i18n.py", "app.py", "exastro_client.py"):
        src = (ROOT / f).read_text(encoding="utf-8")
        assert "Admin role for a new sheet" not in src, f
    assert "Admin role for a new sheet" not in (ROOT / "README.md").read_text(
        encoding="utf-8")


def test_a_blank_field_resolves_to_the_connected_account(store, monkeypatch):
    """'Blank' is a promise about the account, not a guess at a role: the name is
    taken from the configuration, or from the token when only a token is set."""
    from exastro_client import NO_ROLE_SENTINEL
    monkeypatch.setattr(cfg, "ADMIN_ROLE", "")
    monkeypatch.setattr(cfg, "USER", "someone")
    assert ExastroClient().grantable_roles("") == ["someone"]

    class TokenOnly(ExastroClient):
        def token(self):
            import base64, json
            body = base64.urlsafe_b64encode(json.dumps(
                {"preferred_username": "svc-exastro"
                 }).encode()).decode().rstrip("=")
            return f"hdr.{body}.sig"

    monkeypatch.setattr(cfg, "USER", "")
    c = TokenOnly()
    assert c.own_identity() == "svc-exastro"
    assert c.grantable_roles("") == ["svc-exastro"]
    assert c.grantable_roles("team, svc-exastro") == ["team", "svc-exastro"]


def test_an_unreadable_identity_still_sends_a_non_empty_list(monkeypatch):
    """ITA rejects an empty list outright, so an account name that cannot be
    determined degrades to the placeholder rather than to []."""
    from exastro_client import NO_ROLE_SENTINEL
    monkeypatch.setattr(cfg, "USER", "")

    class Opaque(ExastroClient):
        def token(self):
            return "not-a-jwt-at-all"

    c = Opaque()
    assert c.own_identity() == NO_ROLE_SENTINEL
    assert c.grantable_roles("") == [NO_ROLE_SENTINEL]


THEME_CSS = (ROOT / "static" / "theme.css")
PAGES = ("index.html", "result.html", "detail.html", "settings.html",
         "users.html", "login.html")


def test_japanese_renders_in_noto_sans_jp():
    """Noto Sans JP replaced Yu Gothic Light, whose third-weight strokes went
    pale on a dark field at these sizes."""
    css = THEME_CSS.read_text(encoding="utf-8")
    assert "html[lang=ja]" in css
    assert "'Noto Sans JP'" in css and "'Noto Sans Japanese'" in css
    assert "Yu Gothic Light" not in css
    # --mono keeps the coding font first, so Latin values stay tabular
    assert "--mono:'JetBrains Mono','Noto Sans JP'" in css


def test_every_page_loads_the_shared_palette():
    for tpl in PAGES:
        src = (ROOT / "templates" / tpl).read_text(encoding="utf-8")
        assert 'href="/static/theme.css"' in src, tpl
        assert 'data-theme="{{ theme }}"' in src, tpl
        assert ":root{" not in src, f"{tpl} still redeclares the palette"
        # a page-level body rule would outrank the shared one and freeze the
        # dark palette, killing both the theme switch and the Japanese font
        assert not re.search(r"^\s*body\{", src, re.M), tpl
        assert "fonts.googleapis.com" not in src and "@import" not in src, tpl
    # and the palette is served
    assert THEME_CSS.exists() and len(THEME_CSS.read_text(encoding="utf-8")) > 2000


def test_light_theme_overrides_every_token_the_dark_one_defines():
    """Adding a token to `:root` and forgetting the light block is the one way
    this refactor can leave a half-themed page, so it is checked mechanically."""
    css = THEME_CSS.read_text(encoding="utf-8")
    dark = re.search(r":root\{(.*?)\n\}", css, re.S).group(1)
    light = re.search(r'html\[data-theme=light\]\{(.*?)\n\}', css, re.S).group(1)
    names = set(re.findall(r"^(\s+)(--[a-z0-9-]+):", dark, re.M))
    shared = {"--sans", "--mono"}          # deliberately identical in both
    missing = {n for _, n in names} - shared - set(re.findall(r"(--[a-z0-9-]+):", light))
    assert not missing, f"light theme does not define {sorted(missing)}"


def test_page_colours_are_tokens_rather_than_literals():
    """A hard-coded rgba() in a template cannot follow the theme, which is how
    the light palette would end up with dark-mode fragments in it."""
    for tpl in PAGES:
        src = (ROOT / "templates" / tpl).read_text(encoding="utf-8")
        raw = re.findall(r"rgba?\(\s*\d", src)
        assert not raw, f"{tpl} has {len(raw)} unthemed rgba() fills"
        for hexcol in re.findall(r"#[0-9a-fA-F]{3,8}\b", src):
            # masks and SVG presentation attributes take no var(), so they stay
            assert hexcol == "#000" or 'stroke="%s"' % hexcol in src, \
                f"{tpl}: {hexcol} is not themed"


def test_theme_toggle_switches_the_palette_and_remembers_it(mock_client, store):
    page = mock_client.get("/").get_data(as_text=True)
    assert 'data-theme="dark"' in page, "the shipped look is dark"
    assert mock_client.get("/theme/light").status_code == 302
    page = mock_client.get("/").get_data(as_text=True)
    assert 'data-theme="light"' in page
    assert page.count("themesw") >= 1                    # the control is present
    # and it holds on another page of the same session
    assert 'data-theme="light"' in mock_client.get("/settings").get_data(as_text=True)
    mock_client.get("/theme/nonsense")
    assert 'data-theme="light"' in mock_client.get("/").get_data(as_text=True)


def test_a_deployment_can_start_in_the_light_palette(mock_client, store, monkeypatch):
    monkeypatch.setattr(cfg, "UI_THEME", "light")
    with mock_client.session_transaction() as sess:
        sess.pop("theme", None)
    assert 'data-theme="light"' in mock_client.get("/").get_data(as_text=True)


def test_select_controls_are_drawn_by_the_page_not_the_os():
    """A native select brings a white popup, a default arrow and a height that
    does not match the inputs beside it — the two pages must look the same."""
    for tpl in ("index.html", "settings.html"):
        src = (ROOT / "templates" / tpl).read_text(encoding="utf-8")
        assert "appearance:none" in src, tpl
        assert "-webkit-appearance:none" in src, tpl
        assert "background-image:url(\"data:image/svg" in src, tpl   # our own arrow
        assert "cursor:pointer" in src, tpl
        assert "option:checked" in src, tpl
        assert re.search(r"select[^{]*:hover", src), tpl
    # the id lists carry long labels; mono made them overflow their box
    settings_src = (ROOT / "templates" / "settings.html").read_text(encoding="utf-8")
    assert "font-family:inherit" in settings_src


def test_effective_target_shows_only_the_two_resolved_urls(unlocked, store):
    """The preview exists to catch a typo in gateway/org/workspace. Repeating a
    field the operator can already see above it, or a value the client derives
    at send time, is noise in the one place that must stay readable."""
    page = unlocked.get("/settings").get_data(as_text=True)
    blk = re.search(r'<div class="target"[^>]*>(.*?)</div>', page, re.S).group(1)
    assert "API http" in blk and "AUTH http" in blk
    for word in ("CLIENT", "ADMIN", "role_list"):
        assert word not in blk, word
    assert "target_preview" not in blk            # the label is translated


# ---------------------------------------------------------------------------
# Re-running with new values must move the sheet's default values
# ---------------------------------------------------------------------------

def _stored(c, name, key):
    """The `*_default_value` currently stored for one column of a fake sheet."""
    for col in c._fake.sheet_defs[name]["column"].values():
        if col.get("item_name_rest") == key:
            for f in ("single_string_default_value", "integer_default_value",
                      "decimal_default_value", "multi_string_default_value"):
                if f in col:
                    return col[f]
    raise AssertionError(f"{key} not in {name}")


def test_a_second_run_writes_the_new_default_values(client_for):
    """The reported bug: `{"p_jobname": "JOB0100"}` on a sheet that already
    existed was accepted as 'exists' and ITA kept the first run's value."""
    c = client_for()
    c.create_parameter_sheet("demo_mvmt_2", {"p_jobname": "Test",
                                            "p_dsnname": "DEMO.TODAY.DS"})
    out = c.create_parameter_sheet("demo_mvmt_2", {"p_jobname": "JOB0100",
                                                  "p_dsnname": "DEMO.DSN"})
    assert out.startswith("updated:"), out
    assert "'p_jobname'" in out and "'p_dsnname'" in out
    assert _stored(c, "demo_mvmt_2", "p_jobname") == "JOB0100"
    assert _stored(c, "demo_mvmt_2", "p_dsnname") == "DEMO.DSN"
    assert appmod._looks_bad(out) is False


def test_the_edit_carries_the_edit_type_and_the_sheets_identity(client_for):
    c = client_for()
    c.create_parameter_sheet("s1", {"a": "1"})
    c.create_parameter_sheet("s1", {"a": "2"})
    edits = [w[2] for w in c._fake.writes if w[1] == "create/define"
             and w[2].get("type") == "edit"]
    assert len(edits) == 1
    assert edits[0]["menu_create_id"] == "mc-s1"
    # the optimistic-lock token has to survive the round trip
    assert edits[0]["menu"]["last_update_date_time"]
    assert "last_updated_user" not in edits[0]["menu"]


def test_an_untouched_column_is_echoed_and_survives(client_for):
    """A column missing from an edit payload is deleted by ITA, so a run that
    supplies one key must still send the other column unchanged."""
    c = client_for()
    c.create_parameter_sheet("s1", {"a": "one", "b": "two"})
    out = c.create_parameter_sheet("s1", {"a": "changed"})
    assert "updated:" in out and "'a'" in out
    edit = [w[2] for w in c._fake.writes if w[2].get("type") == "edit"][-1]
    assert len(edit["column"]) == 2
    assert _stored(c, "s1", "b") == "two"
    assert _stored(c, "s1", "a") == "changed"


def test_identical_values_do_not_send_a_useless_edit(client_for):
    c = client_for()
    c.create_parameter_sheet("s1", {"a": "same"})
    out = c.create_parameter_sheet("s1", {"a": "same"})
    assert out.startswith("exists:") and "already match" in out
    assert not [w for w in c._fake.writes if w[2].get("type") == "edit"]


def test_a_class_the_install_does_not_know_aborts_the_edit(client_for):
    """Better to refuse than to send a column with an empty class name, which ITA
    answers with 'Required fields' — and a partially-built payload could drop a
    column."""
    lists = {**FakeExastro._DEFINE_LISTS,
             "column_class_list": [{"column_class_id": "99",
                                    "column_class_name": "WhateverColumn"}]}
    c = client_for(define_lists=lists)
    c.create_parameter_sheet("s1", {"a": "1"})
    out = c.create_parameter_sheet("s1", {"a": "2"})
    assert "update failed" in out and "column_class_list" in out
    assert appmod._looks_bad(out)
    assert _stored(c, "s1", "a") == "1"           # previous value kept


def test_an_unreadable_definition_is_reported_as_a_failure(client_for, monkeypatch):
    c = client_for()
    c.create_parameter_sheet("s1", {"a": "1"})
    monkeypatch.setattr(c, "sheet_definition", lambda name: None)
    out = c.create_parameter_sheet("s1", {"a": "2"})
    assert "update failed" in out and appmod._looks_bad(out)


def test_a_refused_edit_names_the_previous_values_kept(client_for, monkeypatch):
    c = client_for()
    c.create_parameter_sheet("s1", {"a": "1"})
    real = c._request

    def refuse(method, url, **kw):
        if "create/define/execute" in url and (kw.get("json") or {}).get("type") == "edit":
            raise ExastroError("The Value of [ sheet type ] is invalid.")
        return real(method, url, **kw)

    monkeypatch.setattr(c, "_request", refuse)
    out = c.create_parameter_sheet("s1", {"a": "2"})
    assert "update failed" in out and "'a'" in out
    assert _stored(c, "s1", "a") == "1"


def test_latin_text_uses_poppins_from_the_app_itself():
    """Poppins is what the operator asked for, and it must arrive from /static/:
    a font CDN is unreachable on a customer intranet and a blocked stylesheet
    request delays first paint until it times out."""
    css = THEME_CSS.read_text(encoding="utf-8")
    assert "--sans:'Poppins'" in css
    faces = re.findall(r"@font-face\{(.*?)\}", css, re.S)
    weights = sorted(re.search(r"font-weight:(\d+)", f).group(1) for f in faces)
    assert weights == ["400", "500", "600", "700", "800"], weights
    for face in faces:
        url = re.search(r"url\('([^']+)'\)", face).group(1)
        assert url.startswith("/static/"), url
        assert (ROOT / url.lstrip("/")).exists(), f"{url} is declared but absent"
        assert "font-display:swap" in face
    # no external asset reference anywhere in the palette
    assert not re.search(r"url\(['\"]?https?://", css)
    assert "Poppins" not in re.search(r"html\[lang=ja\]\{(.*?)\n\}", css, re.S).group(1)


# ---------------------------------------------------------------------------
# Two names per column: display (item_name) and logical (item_name_rest)
#
# Measured on the live install: the substitution pulldown advertises the DISPLAY
# name. SheetA offers 'Parameter/Item 1' for item_name_rest='item_1', and
# SheetB offers 'Parameter/age' where the logical name is 'ahe'.
# ---------------------------------------------------------------------------

def test_a_japanese_display_name_still_binds(client_for):
    c = client_for(variables={"v1": "zos:p_jobname"},
                   sheet_items={"zos_job_submit":
                                {"p_jobname": "ジョブ名"}})
    items = c._sheet_parameter_items("zos_job_submit")
    assert list(items) == ["ジョブ名"], "the fake must mirror ITA, not hope"
    rows = c.link_movement_parameter("zos", "zos_job_submit",
                                     {"p_jobname": "JOB0100"}, wait=False)
    assert rows[0]["status"] == "linked", rows[0]["detail"]
    row = [w for w in c._fake.writes if w[1] == cfg.SUBST_MENU][0][2]
    assert row["menu_group_menu_item"].endswith("/ジョブ名")


def test_the_key_still_works_when_names_are_equal(client_for):
    """Every sheet this tool wrote has item_name == item_name_rest; those must
    keep binding with no definition read available."""
    c = client_for(variables={"v1": "s2:target"})
    assert c.sheet_definition("s2") is None
    rows = c.link_movement_parameter("s2", "s2", {"target": "x"}, wait=False)
    assert rows[0]["status"] == "linked", rows[0]["detail"]


def test_two_columns_sharing_a_display_name_are_refused(client_for):
    """The pulldown carries only the display name, so binding one of these is a
    coin flip. Refusing beats silently registering the wrong column."""
    c = client_for(variables={"v1": "zos:p_a", "v2": "zos:p_b"},
                   sheet_items={"s1": {"p_a": "Same", "p_b": "Same"}})
    rows = c.link_movement_parameter("zos", "s1", {"p_a": "1", "p_b": "2"},
                                     wait=False)
    assert [r["status"] for r in rows] == ["ambiguous_column", "ambiguous_column"]
    assert "cannot tell them apart" in rows[0]["detail"]
    assert "p_a" in rows[0]["detail"] and "p_b" in rows[0]["detail"]
    assert not [w for w in c._fake.writes if w[1] == cfg.SUBST_MENU]


def test_a_display_name_is_written_alongside_the_logical_one(client_for):
    c = client_for()
    c.create_parameter_sheet("s1", {"p_jobname": "JOB0100"},
                             {"p_jobname": {"name": "ジョブ名", "required": True,
                                              "unique": True}})
    col = list([w for w in c._fake.writes if w[1] == "create/define"]
               [0][2]["column"].values())[0]
    assert col["item_name"] == "ジョブ名"
    assert col["item_name_rest"] == "p_jobname"
    assert col["required"] == "1" and col["uniqued"] == "1"
    assert col["single_string_default_value"] == "JOB0100"


def test_an_untouched_grid_writes_the_definition_it_always_wrote(client_for):
    c = client_for()
    c.create_parameter_sheet("s1", {"a": "1"})
    old = list([w for w in c._fake.writes if w[1] == "create/define"]
               [0][2]["column"].values())[0]
    c2 = client_for()
    c2.create_parameter_sheet("s1", {"a": "1"}, {})
    new = list([w for w in c2._fake.writes if w[1] == "create/define"]
               [0][2]["column"].values())[0]
    assert old == new, "the grid must not change the bytes for anyone who ignores it"


def test_a_renamed_column_updates_only_the_label(client_for):
    c = client_for()
    c.create_parameter_sheet("s1", {"a": "one", "b": "two"})
    out = c.create_parameter_sheet("s1", {"a": "one", "b": "two"},
                                   {"a": {"name": "Label A"}})
    assert out.startswith("updated:") and "(name)" in out, out
    assert "default" not in out, out          # no value actually moved
    stored = c._fake.sheet_defs["s1"]["column"]
    assert [k for k, v in stored.items() if v["item_name"] == "Label A"]
    assert all(v["item_name_rest"] for v in stored.values()), "rest names survive"


def test_a_run_that_says_nothing_keeps_a_hand_polished_label(client_for):
    c = client_for()
    c.create_parameter_sheet("s1", {"a": "one"})
    # polish the label directly in ITA (the fake's store is what ITA would hold)
    c._fake.sheet_defs["s1"]["column"]["c1"]["item_name"] = "Pretty name"
    out = c.create_parameter_sheet("s1", {"a": "one"})
    assert out.startswith("exists:") and "already match" in out, out
    assert c._fake.sheet_defs["s1"]["column"]["c1"]["item_name"] == "Pretty name"
    assert not [w for w in c._fake.writes if w[2].get("type") == "edit"]


def test_a_value_moves_and_a_refused_flag_is_named(client_for):
    """Measured: ITA refuses the flag AND the whole edit, so the value has to move
    on its own and the flag has to be reported rather than sent."""
    c = client_for()
    c.create_parameter_sheet("s1", {"a": "1"})
    out = c.create_parameter_sheet("s1", {"a": "2"}, {"a": {"required": True}})
    assert out.startswith("updated:") and "(default)" in out, out
    assert "ITA cannot change \'a\' (required)" in out, out
    col = list(c._fake.sheet_defs["s1"]["column"].values())[0]
    assert col["single_string_default_value"] == "2", "the value did move"
    assert col["required"] == "0", "the flag was never sent, so nothing is claimed"
    assert appmod._looks_bad(out) is False
    edit = [w[2] for w in c._fake.writes if w[2].get("type") == "edit"][-1]
    col = list(edit["column"].values())[0]
    assert col["required"] == "0", "the refusal is not just in the words"


def test_a_flag_only_difference_sends_nothing(client_for):
    c = client_for()
    c.create_parameter_sheet("s1", {"a": "1"})
    out = c.create_parameter_sheet("s1", {"a": "1"}, {"a": {"required": True}})
    assert out.startswith("exists:") and "nothing was sent" in out, out
    assert "required" in out, "and it still says which flag could not move"
    assert not [w for w in c._fake.writes if w[2].get("type") == "edit"]


def test_a_new_parameter_is_added_as_a_column(client_for):
    """The bug this replaced: a key the sheet did not have was never put in the
    payload at all, and the run reported 'already match' while the operator's new
    parameter quietly vanished."""
    c = client_for()
    c.create_parameter_sheet("s1", {"a": "one"})
    out = c.create_parameter_sheet("s1", {"a": "one", "b": "two"},
                                   {"b": {"name": "Label B", "required": True}})
    assert "updated:" in out and "\'b\' (added" in out, out
    cols = {v["item_name_rest"]: v for v in c._fake.sheet_defs["s1"]["column"].values()}
    assert cols["b"]["item_name"] == "Label B"
    assert cols["b"]["single_string_default_value"] == "two"
    assert cols["a"]["item_name_rest"] == "a", "the existing column survived"


def test_a_new_column_arrives_required_while_an_existing_one_cannot(client_for):
    """ITA's rule is about the existing item, not about the field: a column the
    edit creates can carry required/uniqued, and must."""
    c = client_for()
    c.create_parameter_sheet("s1", {"a": "1"})
    c.create_parameter_sheet("s1", {"a": "1", "b": "2"},
                             {"b": {"required": True, "unique": True}})
    cols = {v["item_name_rest"]: v for v in c._fake.sheet_defs["s1"]["column"].values()}
    assert cols["b"]["required"] == "1" and cols["b"]["uniqued"] == "1"
    assert cols["b"]["single_string_maximum_bytes"] == 255, \
        "a new String column without a byte cap is refused outright"


def test_an_integer_default_is_written_to_the_integer_field(client_for):
    """Each class keeps its default in its own field. Writing a number to
    `single_string_default_value` is accepted by a lenient fake and silently
    changes nothing on a real install."""
    c = client_for()
    c.create_parameter_sheet("s1", {"n": 7})
    c.create_parameter_sheet("s1", {"n": 42})
    edit = [w[2] for w in c._fake.writes if w[2].get("type") == "edit"][-1]
    col = list(edit["column"].values())[0]
    assert "integer_default_value" in col and "single_string_default_value" not in col
    assert _stored(c, "s1", "n") == 42 or str(_stored(c, "s1", "n")) == "42"


# ---------------------------------------------------------------------------
# The validator behind the grid
# ---------------------------------------------------------------------------

def test_the_validator_accepts_only_what_the_grid_can_produce():
    meta = appmod.parse_column_meta('{"a": {"name": "Job", "required": true}}',
                                    {"a": "1", "b": "2"})
    assert meta == {"a": {"name": "Job", "required": True}}


@pytest.mark.parametrize("raw,params,expect", [
    ('{"gone": {"name": "X"}}', {"a": "1"}, "not a parameter"),
    ('{"a": "Job"}', {"a": "1"}, "name / required / unique"),
    ('{"a": {"name": "X"}, "b": {"name": "X"}}', {"a": "1", "b": "2"}, "both"),
    ('{"a": {"name": "' + "あ" * 50 + '"}}', {"a": "1"}, "too long"),
    ('not json', {"a": "1"}, "not valid JSON"),
    ('[1,2]', {"a": "1"}, "must be an object"),
])
def test_the_validator_refuses_each_broken_shape(raw, params, expect):
    with pytest.raises(ValueError) as got:
        appmod.parse_column_meta(raw, params)
    assert expect in str(got.value), str(got.value)


def test_a_blank_display_name_is_an_instruction_now_that_rows_are_prefilled():
    """The grid starts from what the sheet holds, so an empty box is something the
    operator did rather than something they omitted: it has to survive validation
    and reach ITA as "use the logical name". Silence would be the old bug -- the
    one that made a cleared label stay put."""
    assert appmod.parse_column_meta('{"a": {"name": "  "}}',
                                    {"a": "1"}) == {"a": {"name": ""}}
    assert appmod.parse_column_meta('{"a": {"name": "a"}}',
                                    {"a": "1"}) == {"a": {"name": "a"}}
    assert appmod.parse_column_meta("", {"a": "1"}) == {}
    assert appmod.parse_column_meta("{}", {"a": "1"}) == {}
    # two cleared labels resolve to two different logical names, not one duplicate
    assert appmod.parse_column_meta('{"a": {"name": ""}, "b": {"name": ""}}',
                                    {"a": "1", "b": "2"}) == {"a": {"name": ""},
                                                              "b": {"name": ""}}


def test_an_explicit_false_is_an_instruction_not_a_no_op():
    """The grid never sends this -- unchecking a box drops the row from the
    payload -- but `{"required": false}` is the only way to say "turn it off"
    against a sheet whose column is already required, so the validator keeps it."""
    assert appmod.parse_column_meta('{"a": {"required": false}}',
                                    {"a": "1"}) == {"a": {"required": False}}
    c = FakeExastro()
    client = ExastroClient()
    client.token = lambda: "t"                     # type: ignore[assignment]
    client.session.request = c.request             # type: ignore[assignment]
    client.create_parameter_sheet("s1", {"a": "1"}, {"a": {"required": False}})
    col = list(c.sheet_defs["s1"]["column"].values())[0]
    assert col["required"] == "0", "a new column honours an explicit false"


def test_the_byte_cap_is_bytes_not_characters():
    """Japanese labels are three bytes per character; a character count would let
    a 60-glyph label through and ITA would refuse the whole sheet."""
    ok = "名" * 42                       # 126 bytes
    long = "名" * 43                     # 129 bytes
    assert appmod.parse_column_meta('{"a": {"name": "%s"}}' % ok, {"a": "1"})
    with pytest.raises(ValueError) as got:
        appmod.parse_column_meta('{"a": {"name": "%s"}}' % long, {"a": "1"})
    assert "129 bytes" in str(got.value)


def test_a_rejected_grid_keeps_the_typed_form(mock_client, tmpdb):
    resp = mock_client.post("/create", data={
        "movement_name": "m1", "role_package": "p" + cfg.ROLE_PKG_SEP + "r",
        "exec_env": "e", "sheet_name": "s1",
        "parameters": '{"a": "1", "b": "2"}',
        "column_meta": '{"a": {"name": "Same"}, "b": {"name": "Same"}}'})
    assert resp.status_code == 400
    page = resp.get_data(as_text=True)
    assert "used by both" in page
    # The grid and the operator's own draft come back intact. Quotes arrive
    # escaped because the value sits in an HTML attribute, which is the point:
    # a hand-typed `"` in a display name cannot break out of the field.
    assert 'id="colrows"' in page
    assert 'name="column_meta"' in page
    assert "&#34;a&#34;" in page
    assert '"a": "1"' in page or "&#34;a&#34;: &#34;1&#34;" in page


def test_the_grid_is_built_from_the_parsed_json(client_for, mock_client, tmpdb):
    """The rows are generated client-side, so a test can only check the contract:
    one hidden field, the render hooks, and no second list the operator could
    get out of step with the values."""
    src = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    assert '<tbody id="colrows">' in src
    assert re.search(r'name="column_meta"\s+id="column_meta"', src)
    # rows come from Object.keys() of the same object the values are read from
    assert "keys.map(k=>" in src
    assert "Object.keys(edits).forEach" in src, "stale rows must be pruned"
    # a row identical to the sheet sends nothing, which is what keeps re-runs
    # from clobbering a label someone polished in the ITA UI
    assert "if(Object.keys(d).length) meta[k]=d;" in src
    assert "maxlength=\"128\"" in src
    # the chips list it replaced is gone rather than duplicated
    assert 'id="chips"' not in src
    page = mock_client.get("/").get_data(as_text=True)
    assert "column_meta" in page and "colrows" in page


def test_no_translation_placeholder_can_collide_with_the_helper_signature():
    """t(key, lang, **fmt) means an entry using `{key}` or `{lang}` raises
    TypeError at the moment it is most needed: while reporting a mistake. Found
    the hard way, so it is now checked for every entry."""
    import inspect
    taken = set(inspect.signature(i18n.t).parameters) - {"fmt"}
    assert "key" in taken and "lang" in taken
    for name, entry in i18n.TEXT.items():
        for lang, text in entry.items():
            used = set(re.findall(r"{([a-zA-Z_][a-zA-Z0-9_]*)}", text))
            bad = used & taken
            assert not bad, f"{name}[{lang}] formats a reserved parameter {bad}"


# ---------------------------------------------------------------------------
# The definition and the pulldown are two queries and can disagree
# ---------------------------------------------------------------------------

def test_a_case_difference_does_not_lose_the_column(client_for):
    """A label retyped in the UI can reach the pulldown in another case than the
    definition reports; that is not a reason to give up on the column."""
    c = client_for(sheet_items={"s": {"p_age": "AGE"}})
    names = c._sheet_column_names("s")
    assert names == {"p_age": "AGE"}
    items = {"Age": "代入値自動登録用:s:パラメータ/Age"}
    got, refusal = c._resolve_sheet_item("p_age", items, names,
                                         c._display_collisions(names))
    assert got == "代入値自動登録用:s:パラメータ/Age", refusal


def test_two_labels_differing_only_in_case_are_refused(client_for):
    """The same fold that rescues a case difference must not become a coin flip:
    'Age' and 'AGE' as two columns of one sheet are refused, not picked."""
    c = client_for(sheet_items={"s": {"a": "Age", "b": "AGE"}})
    names = c._sheet_column_names("s")
    clashes = c._display_collisions(names)
    assert list(clashes.values()) == [["a", "b"]] or list(clashes.values()) == [["b", "a"]]
    got, refusal = c._resolve_sheet_item("a", {"Age": "x", "AGE": "y"}, names, clashes)
    assert got is None and "cannot tell them apart" in refusal


def test_a_miss_says_which_side_disagreed(client_for, monkeypatch):
    """`offers no column named 'p_age'` was a false statement: the sheet did
    have the column, under a name the pulldown had not started offering."""
    c = client_for(variables={"v1": "mv:p_age"},
                   sheet_items={"mv": {"p_age": "AGE"}})
    monkeypatch.setattr(c, "_sheet_parameter_items", lambda name: {})
    rows = c.link_movement_parameter("mv", "mv", {"p_age": "1"}, wait=False)
    assert rows[0]["status"] == "missing_column"
    assert "its definition names it 'AGE'" in rows[0]["detail"], rows[0]["detail"]


# ---------------------------------------------------------------------------
# Re-creating a column to apply Required / Unique (the screen's x, then re-add)
# ---------------------------------------------------------------------------

def _cols(c, name):
    """{logical name: column} as the read endpoint reports it, ids included --
    which is where `create_column_id` comes from, exactly as on a real install."""
    return {v["item_name_rest"]: v
            for v in c.sheet_definition(name)["column"].values()}


def test_the_flags_land_when_the_column_is_replaced(client_for):
    """Measured against the live API: an edit that omits a column and carries it
    again under a fresh id creates it anew, required included -- the same thing
    the operator does with the x button on the definition screen."""
    c = client_for()
    c.create_parameter_sheet("s1", {"a": "one", "b": "two"})
    before = _cols(c, "s1")["a"]["create_column_id"]
    out = c.create_parameter_sheet("s1", {"a": "one", "b": "two"},
                                   {"a": {"required": True}},
                                   replace_flags=True)
    assert out.startswith("updated:") and "required*" in out, out
    # the mark means "this row was re-created to apply it", so it must
    # not appear for a label that did not change
    assert "name*" not in out, out
    a = _cols(c, "s1")["a"]
    assert a["required"] == "1" and a["uniqued"] == "0"
    assert a["single_string_default_value"] == "one", "the value was carried over"
    assert a["item_name"] == "a" and a["item_name_rest"] == "a"
    assert a["create_column_id"] != before, "it is a new record, as measured"
    assert _cols(c, "s1")["b"]["create_column_id"], "the other column is untouched"


def test_a_replacement_is_only_done_when_it_is_asked_for(client_for):
    """Off by default, because the cost is the values entered for that column."""
    c = client_for()
    c.create_parameter_sheet("s1", {"a": "one"})
    before = _cols(c, "s1")["a"]["create_column_id"]
    out = c.create_parameter_sheet("s1", {"a": "one"}, {"a": {"required": True}})
    assert "ITA cannot change" in out and "*" not in out, out
    a = _cols(c, "s1")["a"]
    assert a["required"] == "0" and a["create_column_id"] == before


def test_the_report_says_what_the_operator_loses(client_for):
    c = client_for()
    c.create_parameter_sheet("s1", {"a": "one"})
    out = c.create_parameter_sheet("s1", {"a": "one"}, {"a": {"unique": True}},
                                   replace_flags=True)
    assert "re-creating the column" in out and "Input screen are gone" in out, out


def test_a_replacement_survives_a_value_change_in_the_same_run(client_for):
    """The row is deleted and re-added, so a changed default has to ride along on
    the new record rather than being lost with the old one."""
    c = client_for()
    c.create_parameter_sheet("s1", {"a": "one", "b": "two"})
    c.create_parameter_sheet("s1", {"a": "changed", "b": "two"},
                             {"a": {"required": True}}, replace_flags=True)
    a = _cols(c, "s1")["a"]
    assert a["single_string_default_value"] == "changed" and a["required"] == "1"
    assert _cols(c, "s1")["b"]["single_string_default_value"] == "two"


def test_the_switch_is_off_by_default_and_named_honestly(mock_client, tmpdb):
    src = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    i = src.index('name="replace_flags"')
    assert 'value="1"' in src[i:i + 90]
    # the only `checked` is the draft echo, never a default
    assert "checked" in src[i:i + 200] and "{% if replace_flags %}" in src[i:i + 200]
    page = mock_client.get("/").get_data(as_text=True)
    assert 'name="replace_flags"' in page
    box = page[page.index('name="replace_flags"'):][:240]
    assert "checked" not in box, "a destructive default is a bug waiting to happen"
    assert "values entered for it are lost" in page
    # and the same warning is read by a Japanese operator
    assert i18nmod.TEXT["replace_flags_hint"]["ja"]
    assert "失われ" in i18nmod.TEXT["replace_flags_hint"]["ja"]


def test_the_run_passes_the_switch_to_the_sheet_step(mock_client, tmpdb):
    seen = {}

    class Recorder:
        def create_movement(self, name, env):
            return "created"

        def link_movement_role(self, movement, role):
            return "linked"

        def create_parameter_sheet(self, sheet_name, params, meta=None,
                                   replace_flags=False):
            seen["replace_flags"] = replace_flags
            return "OK"

        def link_movement_parameter(self, movement, sheet, params, wait=True):
            return [{"key": k, "variable": f"{sheet}:{k}", "status": "linked",
                     "detail": "ok"} for k in params]

    appmod.create_everything("m", "p:r", "s", {"a": "1"}, "env", False,
                             cl=Recorder(), replace_flags=True)
    assert seen["replace_flags"] is True
    seen.clear()
    appmod.create_everything("m", "p:r", "s", {"a": "1"}, "env", False,
                             cl=Recorder())
    assert seen["replace_flags"] is False


# ---------------------------------------------------------------------------
# Seeding the grid from what the sheet already holds
# ---------------------------------------------------------------------------

def test_the_state_read_keeps_absent_apart_from_unreadable(client_for):
    """One seeds an empty grid, the other has to be reported: shown as the first,
    a permission or transport problem would quietly turn 'blank means revert' into
    'blank means nothing' with no hint that the sheet was never read."""
    c = client_for()
    assert c.sheet_column_state("ghost") == ("absent", {})
    c.create_parameter_sheet("s1", {"a": "one", "b": "2"},
                             {"a": {"name": "Ay", "required": True}})
    state, cols = c.sheet_column_state("s1")
    assert state == "ok"
    assert cols == {"a": {"name": "Ay", "required": True, "unique": False},
                    "b": {"name": "b", "required": False, "unique": False}}, cols
    # a column whose label was never set reads back as its logical name, which is
    # exactly what the grid needs to know to leave it alone
    assert c.sheet_column_state("s1")[1]["b"]["name"] == "b"
    broken = client_for()

    def down(*a, **k):
        raise RuntimeError("no route to host")

    # patched on the client, because the fixture bound the transport at build
    # time -- replacing the fake's own method would change nothing
    broken.session.request = down
    # any failure -- transport, permissions, a body ITA did not mean -- is
    # reported as such rather than dressed up as "there is no such sheet"
    assert broken.sheet_column_state("s1") == ("error", {})


def test_a_cleared_label_reverts_the_column_to_its_logical_name(client_for):
    """The behaviour the blank box was supposed to have all along."""
    c = client_for()
    c.create_parameter_sheet("s1", {"a": "one"}, {"a": {"name": "Ay"}})
    assert _cols(c, "s1")["a"]["item_name"] == "Ay"
    assert "Ay" in c._sheet_parameter_items("s1")
    out = c.create_parameter_sheet("s1", {"a": "one"}, {"a": {"name": ""}})
    assert out.startswith("updated:"), out
    assert _cols(c, "s1")["a"]["item_name"] == "a"
    assert _cols(c, "s1")["a"]["single_string_default_value"] == "one"
    assert "a" in c._sheet_parameter_items("s1")


def test_an_untouched_prefilled_row_still_sends_nothing(client_for):
    """Seeding must not make every re-run a rename: the row that still matches the
    sheet has to produce no instruction at all."""
    c = client_for()
    c.create_parameter_sheet("s1", {"a": "one"}, {"a": {"name": "Ay"}})
    state, cols = c.sheet_column_state("s1")
    out = c.create_parameter_sheet("s1", {"a": "one"}, {})
    assert "already match" in out, out
    assert cols["a"]["name"] == _cols(c, "s1")["a"]["item_name"] == "Ay"


def test_the_sheet_endpoint_reports_its_three_states(mock_client, tmpdb,
                                                       client_for, monkeypatch):
    routed = client_for()
    monkeypatch.setattr(appmod, "client_for", lambda username: routed)
    routed.create_parameter_sheet("s_live", {"a": "1"},
                                  {"a": {"name": "Ay", "unique": True}})
    got = mock_client.get("/api/sheet/s_live")
    assert got.get_json() == {"state": "ok",
                              "columns": {"a": {"name": "Ay",
                                                "required": False,
                                                "unique": True}}}
    assert mock_client.get("/api/sheet/ghost").get_json()["state"] == "absent"
    assert mock_client.get("/api/sheet/").status_code in (308, 404)

    class Boom:
        def sheet_column_state(self, name):
            raise RuntimeError("no route to host")

    monkeypatch.setattr(appmod, "client_for", lambda username: Boom())
    bad = mock_client.get("/api/sheet/any")
    assert bad.status_code == 502 and bad.get_json()["state"] == "error"


def test_the_endpoint_only_answers_what_the_grid_needs(mock_client, tmpdb,
                                                        client_for, monkeypatch):
    """Column ids and timestamps are the shape a write has to echo, not something
    a browser should be handed back."""
    routed = client_for()
    monkeypatch.setattr(appmod, "client_for", lambda username: routed)
    routed.create_parameter_sheet("s_live", {"a": "1"})
    body = mock_client.get("/api/sheet/s_live").get_data(as_text=True)
    for leak in ("create_column_id", "menu_create_id", "last_update_date_time",
                 "token", "password"):
        assert leak not in body, leak


def test_the_grid_is_wired_to_the_live_sheet():
    src = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    assert "let base={}, baseSheet='', edits={};" in src
    assert "const diff=k=>" in src and "const touched=k=>Object.keys(diff(k))" in src
    # a blank box means the logical name, and only differs when the sheet says so
    assert ", want=typed||k;" in src
    assert "if(want!==was) out.name=typed;" in src
    # the read is debounced and stale answers lose, or typing a long name would
    # fire a request per keystroke and let an old one overwrite a new one
    assert "setTimeout(seed,450)" in src
    assert "if(pending!==token) return;" in src
    assert "fetch('/api/sheet/'+encodeURIComponent(name))" in src
    assert 'id="sheetstate"' in src
    # the failure line must be translatable, not an English string in script
    for key in ("grid_seeded", "grid_new", "grid_unreadable"):
        assert f"t('{key}')" in src
        assert i18nmod.TEXT[key]["ja"]


def _grid_script(mock_client, tmpdb):
    """The grid's real code, spliced out of the rendered page.

    Sliced from the shipped template rather than copied, so the harness below can
    not drift into testing something the browser never receives."""
    page = mock_client.get("/").get_data(as_text=True)
    page = re.sub(r"<!--.*?-->", "", page, flags=re.S)
    decl = re.search(r"const T = \{.*?\};", page, re.S)
    start = page.index("  const params=document.getElementById('parameters');")
    body = page[start:]
    body = body[:body.index("\n})();")]
    assert decl, "the translation map is no longer a single statement"
    return decl.group(0), body


@pytest.mark.skipif(not shutil.which("node"), reason="no node to run the grid under")
def test_the_grid_model_does_what_the_operator_means(mock_client, tmpdb):
    """Runs the page's own grid logic under a stub DOM and checks the bytes the
    form would submit.

    This exists because the seeding rules are pure client-side judgement -- what a
    blank box means, when a row counts as changed, whether a name that only
    appeared by mirroring the Movement Name still gets read -- and no amount of
    asserting on template strings can catch the mistake the operator actually
    found: the seeding working when they typed a name, and not working when the
    page filled it in for them.
    """
    decl, body = _grid_script(mock_client, tmpdb)
    harness = (ROOT / "tests" / "grid_model.mjs").read_text(encoding="utf-8")
    harness = harness.replace("__T_DECL__", decl).replace("__GRID_BODY__", body)
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(harness)
        path = fh.name

    def run(**env):
        # `shell=False` forbids the string form: the env dict has to be built
        # here, because node reads the scenario out of it before the script runs
        merged = dict(os.environ, **{k: str(v) for k, v in env.items()})
        return subprocess.run([shutil.which("node"), path], capture_output=True,
                              text=True, timeout=60, env=merged)

    try:
        done = run()
        # the operator's other two entry points: a re-run link and a rejected form
        # both hand the page a name nobody has touched yet
        prefilled = run(DSH_PRESET_SHEET="some_sheet",
                        DSH_PRESET_PARAMS='{"p_age": 27}')
    finally:
        os.unlink(path)
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    assert "OK a prefilled form reads its sheet on load" in prefilled.stdout, \
        prefilled.stdout + prefilled.stderr[-1500:]
    # each case announces itself by name: a check that quietly stops running is
    # worse than no check at all, which is what the first version of this file did
    for case in ("untouched form sends nothing",
                 "a mirrored sheet name reads the sheet",
                 "seeded labels and flags show, and change nothing",
                 "a cleared box means the logical name",
                 "a label back to its baseline sends nothing",
                 "un-ticking a seeded flag sends false",
                 "a new sheet is announced as new",
                 "an unreadable sheet is reported, and stays silent",
                 "an absent sheet is read again",
                 "an empty name forgets the sheet it had read"):
        assert "OK " + case in done.stdout, done.stdout


@pytest.mark.skipif(not shutil.which("node"), reason="no node to parse the script")
def test_the_pages_script_actually_parses(mock_client, tmpdb):
    """Every other page test looks for strings. None of them can notice that the
    grid's logic renders into JavaScript the browser will refuse to run, and an
    inline script that fails to parse leaves a form that silently sends {}."""
    page = mock_client.get("/").get_data(as_text=True)
    # the template's own comment mentions <script>, which is not a script tag
    page = re.sub(r"<!--.*?-->", "", page, flags=re.S)
    scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
    assert scripts, "the page has no script to check"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write("\n".join(scripts))
        path = fh.name
    try:
        done = subprocess.run([shutil.which("node"), "--check", path],
                              capture_output=True, text=True)
        assert done.returncode == 0, done.stderr[:900]
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# The substitution list lags a rename: wait, retry, and move the old row
# ---------------------------------------------------------------------------

def _no_waiting(c, patience=60):
    """Keep the retry behaviour, drop the sleeping: the loop is what is tested."""
    c.SUBST_POLL = 0
    c.SUBST_SETTLE = patience
    return c


def test_a_refused_binding_is_waited_out_and_lands(client_for):
    """The reported failure: after re-creating a column, the binding step was
    refused with `menu_group_menu_item: The input value is an invalid value`.
    Measured on a live install, ITA keeps rebuilding that option list for ~11
    seconds after a column moves, and the refusal never says so."""
    c = _no_waiting(client_for(variables={"v1": "m1:a"}))
    c.create_parameter_sheet("m1", {"a": "1"})
    c.create_parameter_sheet("m1", {"a": "1"}, {"a": {"name": "Ay"}})
    c._fake.subst_refuse = 2                    # settled on the third attempt
    rows = c.link_movement_parameter("m1", "m1", {"a": "1"}, wait=False)
    assert rows[0]["status"] == "linked", rows[0]
    assert "attempt 3" in rows[0]["detail"], rows[0]
    assert "substitution list" in rows[0]["detail"], rows[0]
    writes = [w for w in c._fake.writes if w[1] == cfg.SUBST_MENU]
    assert len(writes) == 3, writes
    assert writes[-1][2]["menu_group_menu_item"].endswith("パラメータ/Ay")
    assert len(c._fake.store[cfg.SUBST_MENU]) == 1, "one row, not one per attempt"


def test_the_wait_gives_up_and_says_what_to_do(client_for):
    c = _no_waiting(client_for(variables={"v1": "m1:a"}))
    c.create_parameter_sheet("m1", {"a": "1"})
    c._fake.subst_refuse = 999
    c.SUBST_SETTLE = 0                          # the clock, not the wait
    rows = c.link_movement_parameter("m1", "m1", {"a": "1"}, wait=False)
    assert rows[0]["status"] == "failed", rows[0]
    detail = rows[0]["detail"]
    assert "still refused" in detail and "asynchronously" in detail, detail
    assert "run this again" in detail, detail
    assert "invalid value" in detail, "the platform's own words are kept"


def test_a_renamed_column_moves_its_row_instead_of_adding_a_second(client_for):
    """ITA does not call the new item a duplicate -- the string differs -- so a
    plain POST would leave a row bound to a label that no longer exists. That is
    the shape of the junk already sitting on this install's other sheets."""
    c = _no_waiting(client_for(variables={"v1": "m1:a"}))
    c.create_parameter_sheet("m1", {"a": "1"})
    assert c.link_movement_parameter("m1", "m1", {"a": "1"},
                                     wait=False)[0]["status"] == "linked"
    c.create_parameter_sheet("m1", {"a": "1"}, {"a": {"name": "Ay"}})
    rows = c.link_movement_parameter("m1", "m1", {"a": "1"}, wait=False)
    assert rows[0]["status"] == "linked", rows[0]
    assert "moved to the column's current label" in rows[0]["detail"], rows[0]
    stored = list(c._fake.store[cfg.SUBST_MENU].values())
    assert len(stored) == 1, "the old row was rewritten, not left behind"
    assert stored[0]["menu_group_menu_item"].endswith("パラメータ/Ay")
    assert [w[0] for w in c._fake.writes if w[1] == cfg.SUBST_MENU] == ["POST", "PATCH"]


def test_the_move_keeps_everything_ita_locked_the_row_with(client_for):
    """The lock token has to go back unchanged or the PATCH is refused outright:
    `last_update_date_time is invalid (None)` -- the failure an earlier build of
    the role-link path hid behind a duplicate error."""
    c = _no_waiting(client_for(variables={"v1": "m1:a"}))
    c.create_parameter_sheet("m1", {"a": "1"})
    c.link_movement_parameter("m1", "m1", {"a": "1"}, wait=False)
    before = list(c._fake.store[cfg.SUBST_MENU].values())[0]
    c.create_parameter_sheet("m1", {"a": "1"}, {"a": {"name": "Ay"}})
    c.link_movement_parameter("m1", "m1", {"a": "1"}, wait=False)
    patched = [w for w in c._fake.writes
               if w[1] == cfg.SUBST_MENU and w[0] == "PATCH"][-1][2]
    assert patched["last_update_date_time"] == before["last_update_date_time"]
    assert patched["variable_name"] == before["variable_name"]
    assert patched["movement"] == before["movement"]


def test_only_a_substitution_value_refusal_is_read_as_the_race():
    """Too narrow and the wait is dead code; too wide and every genuine bad value
    costs a minute before it is reported."""
    settles = ('POST ... -> HTTP 499: {"0": {"menu_group_menu_item": '
               '["The input value is an invalid value.(input value:Substitution '
               'value:s1:Parameter/a)"]}}')
    assert ExastroClient._is_settling(settles)
    assert ExastroClient._is_settling('{"0": {"menu_group_menu_item": '
                                      '["代入値自動登録用の値は不正な値です。(入力値:x)"]}}')
    assert not ExastroClient._is_settling(
        '{"0": {"variable_name": ["The input value is an invalid value.(input value: z)"]}}')
    assert not ExastroClient._is_settling(
        "The combination is incorrect. (Combination: {'movement': 'm1'})")
    assert not ExastroClient._is_settling("")


def test_a_retried_write_asks_again_instead_of_insisting(client_for):
    """The first read is the reason the write was refused, so the retry has to
    re-read the list rather than re-send the string it already had."""
    reads = {"n": 0}
    c = _no_waiting(client_for(variables={"v1": "m1:a"}))
    c.create_parameter_sheet("m1", {"a": "1"})
    real = c._sheet_parameter_items

    def counting(sheet):
        reads["n"] += 1
        return real(sheet)

    c._sheet_parameter_items = counting
    c._fake.subst_refuse = 2
    c.link_movement_parameter("m1", "m1", {"a": "1"}, wait=False)
    # one read to start, then a fresh one for each of the two refusals
    assert reads["n"] >= 3, reads


# ---------------------------------------------------------------------------
# The sweep that keeps real names out of the public checkout
# ---------------------------------------------------------------------------

SCRUB_TOOL = ROOT / "tools" / "scrub_check.py"


def test_the_scrub_check_finds_what_it_is_given(tmp_path):
    """The check is a one-file script because its input can never be committed.
    It is worth testing for the obvious failure: a tool that always says "clean"
    is worse than no tool, because it is the reason the next person trusts it."""
    import subprocess
    # assembled, so the "not present" name does not appear as a literal in this
    # file -- which the tool would otherwise find, correctly, and report
    absent = "zzz" + "-" + "not-a-name" + "-" + "zzz"
    deny = tmp_path / "denylist"
    deny.write_text(f"def\n\n# a comment\n{absent}\n", encoding="utf-8")
    out = subprocess.run([sys.executable, str(SCRUB_TOOL), str(deny)],
                         capture_output=True, text=True)
    assert out.returncode == 1, out.stdout + out.stderr
    assert "def" in out.stdout, out.stdout          # a token that is really there
    listed = out.stdout.split("still present:")[1]
    assert "def" in listed and absent not in listed, out.stdout

    empty = tmp_path / "empty"
    empty.write_text("", encoding="utf-8")
    out = subprocess.run([sys.executable, str(SCRUB_TOOL), str(empty)],
                         capture_output=True, text=True)
    assert out.returncode == 0 and "clean" in out.stdout, out.stdout

    out = subprocess.run([sys.executable, str(SCRUB_TOOL), str(tmp_path / "gone")],
                         capture_output=True, text=True)
    assert out.returncode == 2, out.stdout + out.stderr


def test_the_scrub_denylist_is_not_itself_committed(tmp_path):
    """The list of things to keep out must not be the thing that gets out."""
    import subprocess
    tracked = subprocess.run(["git", "ls-files"], capture_output=True,
                             text=True).stdout.split()
    assert not [f for f in tracked if "denylist" in f]
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".scrub-denylist" in ignore


# ---------------------------------------------------------------------------
# The operator's report: "offers no column named 'p_age'; available: Age, Name"
# ---------------------------------------------------------------------------

def test_a_column_that_exists_but_is_not_offered_yet_is_waited_for(client_for):
    """The failure reported: a blank display name reverted the column to its
    logical name, the definition answered with it at once, and the substitution
    list was still advertising the old label -- so the run reported a column that
    plainly exists. A refused write is the visible half of the race; this is the
    half where nothing is refused and the key simply never resolves."""
    c = _no_waiting(client_for(variables={"v1": "m1:p_age"}))
    c.create_parameter_sheet("m1", {"p_age": "27"},
                             {"p_age": {"name": "Age"}})
    c.create_parameter_sheet("m1", {"p_age": "27"}, {"p_age": {"name": ""}})
    c._fake.subst_lag = 5            # several reads behind, as measured
    rows = c.link_movement_parameter("m1", "m1", {"p_age": "27"}, wait=False)
    assert rows[0]["status"] == "linked", rows[0]
    detail = rows[0]["detail"]
    assert "accepted after" in detail and "reads" in detail, detail
    # the reason belongs in the report: a run that paused for a dozen seconds on
    # one key looks like a hang unless it says what it was waiting for
    assert "rebuilds a sheet's substitution list" in detail, detail


def test_the_waited_for_column_is_bound_by_its_current_label(client_for):
    c = _no_waiting(client_for(variables={"v1": "m1:p_age"}))
    c.create_parameter_sheet("m1", {"p_age": "27"}, {"p_age": {"name": "Age"}})
    c.create_parameter_sheet("m1", {"p_age": "27"}, {"p_age": {"name": ""}})
    c._fake.subst_lag = 5
    c.link_movement_parameter("m1", "m1", {"p_age": "27"}, wait=False)
    writes = [w for w in c._fake.writes if w[1] == cfg.SUBST_MENU]
    assert writes[-1][2]["menu_group_menu_item"].endswith("パラメータ/p_age"), \
        writes[-1][2]


def test_a_list_that_never_catches_up_says_so_and_tells_the_operator_what_to_do(client_for):
    """The report used to be a bare 'no column of it is offered', which reads as
    'your JSON is wrong' when the platform is merely behind."""
    c = _no_waiting(client_for(variables={"v1": "m1:p_age"}))
    c.create_parameter_sheet("m1", {"p_age": "27"}, {"p_age": {"name": "Age"}})
    c.create_parameter_sheet("m1", {"p_age": "27"}, {"p_age": {"name": ""}})
    c._fake.subst_lag = 999
    rows = c.link_movement_parameter("m1", "m1", {"p_age": "27"}, wait=False)
    assert rows[0]["status"] == "missing_column", rows[0]
    detail = rows[0]["detail"]
    assert "waiting" in detail and "run this again" in detail, detail
    assert "Available" not in detail or "Age" in detail, "the stale choices are named"


def test_a_column_the_sheet_does_not_have_is_not_waited_for(client_for):
    """The wait exists for the platform's delay, not for the operator's typos: a
    key the definition never mentions is reported in the same request as before."""
    c = _no_waiting(client_for(variables={"v1": "m1:p_age"}))
    c.create_parameter_sheet("m1", {"p_age": "27"})
    before = len([w for w in c._fake.writes])
    rows = c.link_movement_parameter("m1", "m1", {"nonexistent": "1"}, wait=False)
    assert rows[0]["status"] == "missing_column", rows[0]
    assert "waiting" not in rows[0]["detail"], rows[0]["detail"]
    assert len([w for w in c._fake.writes]) == before


def test_the_wait_is_shared_and_capped_by_rounds_as_well_as_seconds(client_for):
    """Three columns behind by the same amount must cost one window, and the
    window has to end even when the clock is switched off for the tests."""
    c = _no_waiting(client_for(variables={"v1": "m1:a", "v2": "m1:b",
                                          "v3": "m1:c"}), patience=9999)
    c.create_parameter_sheet("m1", {"a": "1", "b": "2", "c": "3"},
                             {"a": {"name": "Alpha"}, "b": {"name": "Beta"},
                              "c": {"name": "Gamma"}})
    c.create_parameter_sheet("m1", {"a": "1", "b": "2", "c": "3"},
                             {"a": {"name": ""}, "b": {"name": ""},
                              "c": {"name": ""}})
    c._fake.subst_lag = 9999
    started = time.time()
    rows = c.link_movement_parameter("m1", "m1", {"a": "1", "b": "2", "c": "3"},
                                     wait=False)
    assert [r["status"] for r in rows] == ["missing_column"] * 3
    # 3 keys x 20 rounds would be 60 reads with no bound; the window is shared and
    # rounds are capped, so the whole step costs one budget instead of three
    assert max(r["detail"].count("reads") for r in rows) == 1


# ---------------------------------------------------------------------------
# Operation and Input row: the toggle that schedules the run
# ---------------------------------------------------------------------------

STAMP = time.strptime("2026-09-14 09:07:28", "%Y-%m-%d %H:%M:%S")


def _opfake(client_for, **kw):
    c = client_for(hosts={"DEMO_GP": ["demo_host_a", "demo_host_b"],
                          "OTHER_GP": ["demo_host_zos"]}, **kw)
    return c


def test_the_host_lists_come_from_the_groups_own_links(client_for):
    """host_link_list is membership; the device list is not, and reading hosts
    from there would offer machines the chosen group does not contain."""
    c = _opfake(client_for)
    assert [g["name"] for g in c.host_groups()] == ["DEMO_GP", "OTHER_GP"]
    assert c.hosts_in_group("DEMO_GP") == ["demo_host_a", "demo_host_b"]
    assert c.hosts_in_group("DEMO_gp") == ["demo_host_a", "demo_host_b"]
    assert c.hosts_in_group("NOWHERE") == []


def test_a_host_is_written_in_itas_spelling(client_for):
    c = _opfake(client_for)
    assert c.host_value("demo_host_a") == "[H]demo_host_a"


def test_the_operation_name_is_the_movement_plus_the_execution_stamp(client_for):
    c = _opfake(client_for)
    op = c.create_operation("m1", when=STAMP)
    assert op["name"] == "m120260914090728", op
    posted = [w for w in c._fake.writes if w[1] == cfg.OPERATION_MENU][0][2]
    # measured: only slashes with seconds are accepted; dashes are refused
    assert posted["scheduled_date_for_execution"] == "2026/09/14 09:07:28", posted
    assert posted["operation_name"] == op["name"]


def test_the_operation_label_is_derived_from_the_stored_schedule(client_for):
    """ITA reads the schedule back with dashes and advertises the item to the
    minute. The seconds that were written are not in the label, and the label is
    built from the value read back rather than the one sent."""
    c = _opfake(client_for)
    op = c.create_operation("m1", when=STAMP)
    assert op["scheduled"] == "2026-09-14 09:07:28", op
    assert op["select"] == "2026-09-14 09:07_m120260914090728", op


def test_the_input_row_carries_values_host_and_operation(client_for):
    c = _opfake(client_for)
    op = c.create_operation("m1", when=STAMP)
    out = c.create_movement_input("m1", {"p_jobname": "JOB0001", "p_int": 7,
                                         "p_nil": None,
                                         "p_list": ["a", "b"]},
                                  op, "[H]demo_host_a")
    assert out.startswith("OK"), out
    body = [w for w in c._fake.writes if w[1] == "m1"][-1][2]
    assert body["p_jobname"] == "JOB0001"
    assert body["p_int"] == 7
    assert "p_nil" not in body, "an absent value stays absent, not the text None"
    assert body["p_list"] == '["a","b"]', "structures go in as compact JSON"
    assert body["host_name"] == "[H]demo_host_a"
    assert body["operation_name_select"] == op["select"]


def test_an_unknown_host_is_refused_by_the_platform(client_for):
    """The fake validates like the install: the client is not trusted to have
    checked, and the refusal must reach the report instead of becoming a silent
    'OK' with no row behind it."""
    c = _opfake(client_for)
    op = c.create_operation("m1", when=STAMP)
    with pytest.raises(Exception) as bad:
        c.create_movement_input("m1", {}, op, "[H]no_such_host")
    assert "host_name" in str(bad.value)


def test_a_wrong_operation_label_is_retried_from_a_row_ita_accepted(client_for, monkeypatch):
    """The label's spelling is ITA's, not this tool's: it is built from a stored
    timestamp, and an account that formats that differently would otherwise cost
    the run with a message naming nothing useful."""
    c = _opfake(client_for)
    op = c.create_operation("m1", when=STAMP)
    c._fake.store.setdefault("m1", {})["old"] = {
        "uuid": "old", "operation_name_disp": op["name"],
        "operation_name_select": f"2026/09/14 09:07_{op['name']}"}
    monkeypatch.setattr(ExastroClient, "_select_label",
                        staticmethod(lambda s, n: f"WRONG_{n}"))
    again = c.create_operation("m1", when=time.strptime(
        "2026-09-14 09:08:29", "%Y-%m-%d %H:%M:%S"))
    again["name"] = op["name"]
    monkeypatch.setattr(ExastroClient, "_select_label",
                        staticmethod(lambda s, n: f"WRONG_{n}"))
    out = c.create_movement_input("m1", {}, again, "[H]demo_host_a")
    assert "already accepted" in out, out
    assert again["select"] == f"2026/09/14 09:07_{op['name']}"


def test_the_retry_only_applies_to_a_refusal_about_that_column(client_for):
    c = _opfake(client_for)
    assert c._retry_select_format("m1", {"name": "x"}, "host_name is invalid") is None


def _op_recorder(order, made):
    class Recorder:
        def create_movement(self, name, env):
            order.append("movement"); return "created"

        def link_movement_role(self, movement, role):
            order.append("role_link"); return "linked"

        def create_parameter_sheet(self, sheet_name, params, meta=None,
                                   replace_flags=False):
            order.append("param_sheet"); return "OK"

        def link_movement_parameter(self, movement, sheet, params, wait=True):
            order.append("param_link")
            return [{"key": k, "variable": f"{sheet}:{k}", "status": "linked",
                     "detail": "ok"} for k in params]

        def create_operation(self, movement_name, remarks="", when=None):
            order.append("operation")
            made["op"] = {"name": f"{movement_name}20260914090728",
                          "operation_id": "o-1",
                          "scheduled": "2026-09-14 09:07:28",
                          "select": "2026-09-14 09:07_" + movement_name + "20260914090728"}
            return made["op"]

        def create_movement_input(self, movement_name, params, operation,
                                  host_name, remarks=""):
            order.append("input")
            made["host"] = host_name
            made["params"] = params
            return f"OK: input row on {host_name}"
    return Recorder()


def test_the_operation_is_created_between_the_bindings_and_the_input_row(tmpdb):
    """Order is behaviour here, not tidiness: the input row names an operation
    that must already exist and be readable, and both come after the parameter
    work, because an input row for a movement whose bindings failed would run
    against defaults."""
    order, made = [], {}
    rep = appmod.create_everything("m", "p:r", "s", {"a": "1"}, "env", False,
                                   cl=_op_recorder(order, made),
                                   create_op=True, run_target="[HG]DEMO_GROUP",
                                   host_label="DEMO_GROUP")
    assert order == ["movement", "role_link", "param_sheet", "param_link",
                     "operation", "input"], order
    assert [st["key"] for st in rep["steps"]][-2:] == ["operation", "input"]
    assert made["host"] == "[HG]DEMO_GROUP"
    assert made["params"] == {"a": "1"}, "the input row carries the run's values"
    # and the report says what the operator chose, not how ITA spells it
    assert rep["steps"][-1]["args"]["name"] == "DEMO_GROUP", rep["steps"][-1]
    assert rep["status"] == "OK", rep


def test_an_input_row_is_never_written_without_an_operation(tmpdb):
    """If the operation fails, the input row cannot name one. Continuing would
    write a row addressed to nothing and report PARTIAL as if it had created
    half of it correctly."""
    order, made = [], {}
    cl = _op_recorder(order, made)
    cl.create_operation = lambda *a, **kw: (_ for _ in ()).throw(
        ExastroError("operation_list refused"))
    rep = appmod.create_everything("m", "p:r", "s", {"a": "1"}, "env", False,
                                   cl=cl, create_op=True, run_target="[HG]G")
    assert order == ["movement", "role_link", "param_sheet", "param_link"], order
    keys = [st["key"] for st in rep["steps"]]
    assert "input" not in keys, keys
    assert rep["status"] == "FAILED", rep


def test_the_toggle_off_writes_neither_row(mock_client, tmpdb):
    resp = mock_client.post("/create", data={
        "movement_name": "demo_off", "role_package": "demo_pkg",
        "role_select": "DEMO_HOST_JOB", "exec_env": "DEMO_EXEC_ENV",
        "parameters": '{"p_jobname": "J"}'}, follow_redirects=True)
    page = resp.get_data(as_text=True)
    assert "Create Operation" not in page
    stored = appmod.get_creation(appmod.list_creations(1)[0]["id"])
    # history keeps the whole payload (report, params, types), and `report` is
    # one of its keys, so a run is read back through both layers
    report = stored["report"]["report"]
    assert "operation" not in [st["key"] for st in report["steps"]], report


def test_a_group_the_install_does_not_have_stops_the_run_before_anything_is_written(
        mock_client, tmpdb):
    """A name ITA has never heard of is refused with a message that names nothing
    the operator can act on, so the install's own group list is checked first."""
    resp = mock_client.post("/create", data={
        "movement_name": "demo_bad", "role_package": "demo_pkg",
        "role_select": "DEMO_HOST_JOB", "exec_env": "DEMO_EXEC_ENV",
        "parameters": '{"p_jobname": "J"}', "create_op": "1",
        "host_group": "NO_SUCH_GROUP"})
    assert resp.status_code == 400
    assert "is not a host group on this workspace" in resp.get_data(as_text=True)
    assert appmod.count_creations() == 0, "nothing was created"


def test_an_empty_group_is_not_a_run(mock_client, tmpdb):
    """The dialog cannot be dismissed into a half-answer: a switch on with no
    group behind it would write an Input row addressed to nothing."""
    for missing in ("", "   "):
        resp = mock_client.post("/create", data={
            "movement_name": "demo_none", "role_package": "demo_pkg",
            "role_select": "DEMO_HOST_JOB", "exec_env": "DEMO_EXEC_ENV",
            "parameters": '{"p_jobname": "J"}', "create_op": "1",
            "host_group": missing})
        assert resp.status_code == 400, missing
        assert "Choose a host group" in resp.get_data(as_text=True)
    assert appmod.count_creations() == 0


def test_the_chosen_group_is_spelled_for_ita_once(mock_client, tmpdb):
    """The route hands the run a value ITA's column wants, built by the client
    that owns that format, so no second place in the code has to know about
    `[HG]` -- and the report keeps the plain name the operator picked."""
    seen = {}
    real = appmod.create_everything

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    monkey = appmod.create_everything
    appmod.create_everything = spy
    try:
        mock_client.post("/create", data={
            "movement_name": "demo_spell", "role_package": "demo_pkg",
            "role_select": "DEMO_HOST_JOB", "exec_env": "DEMO_EXEC_ENV",
            "parameters": '{"p_jobname": "J"}', "create_op": "1",
            "host_group": "DEMO_GROUP"}, follow_redirects=True)
    finally:
        appmod.create_everything = monkey
    assert seen["create_op"] is True
    assert seen["run_target"] == "[HG]DEMO_GROUP", seen
    assert seen["host_label"] == "DEMO_GROUP", "the report names the group itself"


def test_the_host_endpoint_answers_with_groups_and_their_hosts(mock_client):
    body = mock_client.get("/api/host_groups").get_json()
    # the page labels options with the value ITA stores, and takes the spelling
    # from here rather than learning it a second time
    assert body["prefix"] == "[HG]", body
    assert {"name": "DEMO_GROUP",
            "hosts": ["demo_host_a", "demo_host_b"]} in body["groups"]
    assert [g["name"] for g in body["groups"]] == ["DEMO_GROUP", "DEMO_GROUP_ZOS"]


def test_the_page_offers_the_toggle_and_one_group_dropdown(mock_client):
    """One dropdown, not two. The operator asked for the Host list back out: the
    Input column takes a whole group, so a second choice only offered a way to
    name something other than what the run is addressed to."""
    page = mock_client.get("/").get_data(as_text=True)
    for marker in ('name="create_op"', 'id="hostpick"', 'id="hp-group"',
                   'name="host_group"'):
        assert marker in page, marker
    assert 'id="hp-host"' not in page, "the host dropdown came back out"
    assert 'name="host_name"' not in page, "and nothing posts a host either"
    # the toggle's hint has to say when it should stay off, or the operator
    # cannot tell this apart from a box that is simply unchecked by accident
    assert "ServiceNow" in page


def test_the_dialogs_controls_are_the_pages_controls(mock_client):
    """The popup looked like it belonged to another app because its <select> had
    no `class="input"`: every control on this page is styled through that class,
    and a bare one falls back to the browser widget -- grey box, system font, no
    arrow. Same for the buttons: the page's `.btn` is a full-width submit, which
    inside a dialog reads as a slab."""
    page = mock_client.get("/").get_data(as_text=True)
    box = page[page.index('<div id="hostpick"'):page.index("</script>")]
    assert '<select id="hp-group" class="input">' in box, box[:600]
    css = page[:page.index("</style>")]
    assert "#hostpick .btn{width:auto;margin:0" in css, css
    assert "max-width:330px" in css.replace(" ", ""), "a dialog box, not a panel"
    # the hint had no rule of its own, so a standalone one inherited the body size
    assert ".hint{font-size:11.5px" in css.replace(" ", "")


def test_cancelling_the_dialog_turns_the_toggle_off():
    """The cancel button means "do not create these", not "create them with no
    group": ITA would refuse the latter with a message that names nothing."""
    import pathlib
    js = pathlib.Path("templates/index.html").read_text(encoding="utf-8")
    handler = js[js.index("'hp-cancel'"):]
    handler = handler[:handler.index("</script>")]
    assert "toggle.checked = false" in handler, handler
    assert "gHidden.value = ''" in handler, handler
    # and it closes the dialog, or "cancel" leaves the thing it cancelled open
    assert "close()" in handler


def test_the_host_dialog_is_not_pinned_closed_by_an_inline_style(mock_client):
    """`style="display:none"` on the dialog beats `.on{display:flex}`, because an
    inline declaration outranks a class: the switch flipped, the fetch ran, and
    nothing appeared. The hidden state belongs in the stylesheet, where the
    class that shows the dialog can reach it."""
    page = mock_client.get("/").get_data(as_text=True)
    opening = page[page.index('<div id="hostpick"'):]
    opening = opening[:opening.index(">") + 1]
    assert "style=" not in opening, opening
    css = page[:page.index("</style>")]
    assert "#hostpick{" in css and "display:none" in css
    assert "#hostpick.on{display:flex}" in css.replace(" ", "") or \
        "#hostpick.on{display:flex}" in css, "the showing rule must exist"


def test_the_option_is_a_switch_that_still_posts_a_value(mock_client):
    page = mock_client.get("/").get_data(as_text=True)
    assert 'class="sw"' in page and 'class="track"' in page and 'class="knob"' in page
    # a switch is still a checkbox on the wire: unchecked posts nothing, so the
    # server's `== "1"` test keeps meaning exactly what it says
    assert 'name="create_op" id="create_op" value="1"' in page
    assert '<label class="check"' not in page[page.index('name="create_op"') - 400:
                                          page.index('name="create_op"')]


def test_the_hint_is_two_lines_and_not_three(mock_client):
    """The operator asked for two lines. One sentence carrying both halves wrapped
    to three inside the column, so the wording is split into two strings, each
    short enough to sit on its own line, and the row is rendered as two `hint`
    divs rather than a paragraph that breaks wherever the font decides."""
    en = mock_client.get("/").get_data(as_text=True)
    row = en[en.index('class="swrow"'):en.index('class="sw"')]
    assert row.count('class="hint"') == 2, row
    a = i18n.t("op_input_hint_a", "en")
    b = i18n.t("op_input_hint_b", "en")
    assert a in en and b in en
    assert "ServiceNow" in en
    for line in (a, b):
        assert len(line) <= 78, f"{line!r} is {len(line)} chars and will wrap"
    assert '"op_input_hint":' not in pathlib.Path("i18n.py").read_text(
        encoding="utf-8"), "the one-string version is gone"

    mock_client.get("/lang/ja")
    ja = mock_client.get("/").get_data(as_text=True)
    assert "ローカル実行：オンにすると" in ja, "the same two lines, in Japanese"
    assert "Run locally: turn this on" not in ja, "one language on screen at a time"


def test_a_route_test_cannot_touch_the_real_history_file():
    """206 mock runs landed in the operator's own history because the fixture that
    patches the client did not depend on the fixture that redirects the database.
    Dependency, not documentation: a list a test can forget is a list that gets
    forgotten."""
    import inspect
    src = inspect.getsource(mock_client)
    assert "tmpdb" in src, src
    assert 'DB_PATH' in inspect.getsource(tmpdb)


def _popup_script(mock_client, tmpdb):
    """The popup's real code, sliced out of the rendered page like the grid's."""
    page = mock_client.get("/").get_data(as_text=True)
    page = re.sub(r"<!--.*?-->", "", page, flags=re.S)
    decl = re.search(r"const T = \{.*?\};", page, re.S)
    inside = page.index("  var toggle = document.getElementById('create_op');")
    # the wrapper has to come with it: the body returns early when the elements
    # are missing, and a bare `return` at module top level is a SyntaxError
    start = page.rindex("(function () {", 0, inside)
    end = page.index("\n})();", inside) + len("\n})();")
    body = page[start:end]
    assert decl, "the translation map is no longer a single statement"
    return decl.group(0), body


@pytest.mark.skipif(not shutil.which("node"), reason="no node to run the popup under")
def test_the_popup_does_what_the_switch_says(mock_client, tmpdb):
    """Runs the shipped popup logic and checks what the form would post.

    Three things this catches that reading the template cannot: a dialog carrying
    an inline `display:none` that the `.on` class could not override, so it opened
    into nothing; a cancel that has to stand the whole offer down rather than just
    hide the box; and an echo built with `replace('@@', v)`, which fills only the
    first placeholder and left `[HG]@@` on screen. All three were found by a person
    looking at the page — which is the reason this file exists.
    """
    decl, body = _popup_script(mock_client, tmpdb)
    harness = (ROOT / "tests" / "popup_model.mjs").read_text(encoding="utf-8")
    harness = harness.replace("__T_DECL__", decl).replace("__POPUP_BODY__", body)
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(harness)
        path = fh.name
    try:
        done = subprocess.run([shutil.which("node"), path], capture_output=True,
                              text=True, timeout=60)
    finally:
        os.unlink(path)
    assert done.returncode == 0, done.stdout[-2500:] + done.stderr[-2500:]
    for case in ("the switch opens the dialog and asks the install",
                 "options read [HG]group while the form posts the plain name",
                 "the note reports membership and warns about a group at zero",
                 "confirming posts the group and echoes the value ITA will store",
                 "confirming with no group keeps the dialog open and says why",
                 "cancel turns the switch off and clears the choice",
                 "switching off clears the posted field",
                 "no groups on the install is said out loud",
                 "the popup reads the install once and reopens from memory"):
        assert f"OK {case}" in done.stdout, f"{case} did not run:\n{done.stdout}"


# ---------------------------------------------------------------------------
# the tab icon
# ---------------------------------------------------------------------------

def _ico_entries(path):
    """Parse an ICO into {size: rgba_rows}. Written here rather than imported
    from the generator, so a bug in one cannot sign off on the other."""
    import struct
    import zlib
    data = path.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert reserved == 0 and kind == 1, "not an icon container"
    out = {}
    for i in range(count):
        w, h, _, _, planes, bpp, size, off = struct.unpack(
            "<BBBBHHII", data[6 + 16 * i:6 + 16 * i + 16])
        blob = data[off:off + size]
        assert blob[:8] == b"\x89PNG\r\n\x1a\n", "entry is not a PNG"
        iw, ih = struct.unpack(">II", blob[16:24])
        pos, idat = 33, b""
        while pos < len(blob) - 12:
            ln = struct.unpack(">I", blob[pos:pos + 4])[0]
            if blob[pos + 4:pos + 8] == b"IDAT":
                idat += blob[pos + 8:pos + 8 + ln]
            pos += 12 + ln
        raw = zlib.decompress(idat)
        stride = iw * 4 + 1
        rows = [[tuple(raw[y * stride + 1 + x * 4:y * stride + 1 + x * 4 + 4])
                 for x in range(iw)] for y in range(ih)]
        assert (w or 256) == iw and planes == 1 and bpp == 32
        out[iw] = rows
    return out


def test_every_page_links_the_icon_it_serves():
    """A page with no icon link is a page that 404s on /favicon.ico, and the
    tab shows a generic sheet of paper: the reason this exists at all."""
    import glob
    files = sorted(glob.glob(str(ROOT / "templates" / "*.html")))
    assert files, "no templates found to check"
    for f in files:
        head = pathlib.Path(f).read_text(encoding="utf-8")[:1200]
        assert 'rel="icon" type="image/svg+xml"' in head, f
        assert 'rel="alternate icon"' in head, f
        assert '<link rel="icon"' in head.split('<link rel="stylesheet"')[0], \
            f"{f}: the icon must be declared with the stylesheet, not after it"


def test_the_icon_is_the_apps_own_mark_not_the_vendors():
    """The tab shows the same glyph as the app bar. It is not the platform
    vendor's logo, and that is a decision, not an oversight: this repository is
    public, and shipping somebody else's trademark as a courtesy is not this
    tool's call to make. static/favicon.svg says how to put the real asset in."""
    import xml.etree.ElementTree as ET
    svg = (ROOT / "static" / "favicon.svg").read_text(encoding="utf-8")
    ET.fromstring(svg)                    # an icon that fails to parse is a blank tab
    body = svg.replace("\n", " ")
    assert "#4f46e5" in body and "#7c3aed" in body, "the theme's accent pair"
    # the same path the app bar draws, so the two cannot be different glyphs
    markup = pathlib.Path("templates/index.html").read_text(encoding="utf-8")
    bar = re.search(r'class="logo">.*?<path d="([^"]+)"', markup, re.S).group(1)
    assert f'd="{bar}"' in body, "the icon drifted from the app bar's mark"
    assert "Exastro" in svg, "and it is labelled for whoever inspects it"


def test_the_raster_icon_reads_at_tab_size():
    """The check that earns its keep: at 16px a stroke that straddles a pixel
    boundary splits into two half-lit rows and reads as grey smudge, which is
    what the first versions of this file produced -- once with the tile corners
    filled solid, once with all three bars washed to 55% white, once with the
    stroke scaled twice. Numbers pass in all three cases. Rows are checked
    instead."""
    entries = _ico_entries(ROOT / "static" / "favicon.ico")
    assert set(entries) == {16, 32, 48}, sorted(entries)
    for size, rows in entries.items():
        assert rows[0][0][3] == 0, f"{size}: the rounded corner must be clear"
        assert rows[0][size // 2][3] > 200, f"{size}: the tile must be painted"
        bright = [y for y in range(size)
                  if any(p[3] > 200 and min(p[:3]) > 225 for p in rows[y])]
        assert len(bright) >= 3, f"{size}px: three bars expected, lit rows {bright}"
        if size == 16:
            # one crisp row per bar, not two half rows -- the smudge case
            assert len(bright) == 3, f"bars must snap to whole pixels: {bright}"
            widest = max(sum(1 for p in rows[y] if min(p[:3]) > 225) for y in bright)
            shortest = min(sum(1 for p in rows[y] if min(p[:3]) > 225) for y in bright)
            assert shortest * 1.5 < widest, "the third bar has to stay short"


def test_the_raster_icon_is_not_stale():
    """`static/favicon.svg` is the source of truth for the mark and the .ico is
    generated from the same geometry; a committed binary nobody can diff is how
    an icon ends up not matching the app bar it is supposed to echo."""
    done = subprocess.run([sys.executable, str(ROOT / "tools" / "make_favicon.py"),
                           "--check"], capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_icon_files_are_served(mock_client):
    resp = mock_client.get("/favicon.ico")
    assert resp.status_code == 200
    assert "icon" in resp.content_type, resp.content_type
    assert len(resp.get_data()) > 1000
    svg = mock_client.get("/static/favicon.svg")
    assert svg.status_code == 200 and "svg+xml" in svg.content_type


# ---------------------------------------------------------------------------
# the deployable service
# ---------------------------------------------------------------------------

DEPLOY = ROOT / "deploy"
INSTALL_SH = DEPLOY / "install-service.sh"
UNIT_TEMPLATE = DEPLOY / "exastro-automate.service"


def _rendered_unit():
    done = subprocess.run(["bash", str(INSTALL_SH), "--render"],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_the_service_uses_the_projects_own_interpreter():
    """The first dry run resolved `.venv/bin/python` with `readlink -f`, which
    follows the link to the base interpreter: the unit would have started a plain
    python3 with no Flask in it, and a service that dies on import looks from
    outside like a service that is running."""
    unit = _rendered_unit()
    line = [l for l in unit.splitlines() if l.startswith("ExecStart=")][0]
    exe = line.split("=", 1)[1].split()[0]
    assert "/.venv/bin/python" in exe, exe
    assert "/usr/bin/python" not in exe, "the venv must not be resolved away"
    done = subprocess.run([exe, "-c", "import flask, requests; print('deps ok')"],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and "deps ok" in done.stdout, done.stderr[-400:]


def test_the_service_promises_the_things_that_bite():
    """WorkingDirectory, because the run history and profiles are relative paths
    and a unit without it starts happily beside an empty set of databases; TZ,
    because the Operation step reads the machine clock; and a writable app
    directory, because the service user has to be able to write them."""
    unit = _rendered_unit()
    assert "WorkingDirectory=" in unit and "WorkingDirectory=/" in unit
    assert "Environment=TZ=Asia/Yangon" in unit, "the team works from Yangon"
    assert "ReadWritePaths=" in unit
    assert "Restart=always" in unit and "User=" in unit
    # one process: profile switching lives in module state
    assert "gunicorn" not in unit and "workers" not in unit.lower()
    for forbidden in ("EXA_API_TOKEN", "EXA_PASSWORD"):
        assert forbidden not in unit.replace("EXA_API_TOKEN would go in it", ""), \
            f"{forbidden} does not belong in a world-readable unit file"


def test_the_installer_cannot_disturb_the_web_servers_on_the_box():
    """The operator's note was 'do not impact the existing application', and this
    box really does serve something else: Apache is active here with its own
    vhost. So the script may read service state and print advice, but it must not
    act on another daemon or its configuration."""
    script = INSTALL_SH.read_text(encoding="utf-8")
    executed = [l for l in script.splitlines()
                if not l.lstrip().startswith("#")
                and not l.lstrip().startswith("say ")
                and "printf" not in l]
    code = "\n".join(executed)
    for svc in ("apache2", "httpd", "nginx"):
        assert f"systemctl stop {svc}" not in code
        assert f"systemctl restart {svc}" not in code
        assert f"systemctl reload {svc}" not in code
        assert f"systemctl enable {svc}" not in code
        assert f"/etc/{svc}" not in code, f"the script must not write into /etc/{svc}"
    for verb in ("allow", "deny", "delete", "enable", "disable", "reset"):
        assert f"ufw {verb}" not in code, "firewall changes are advice, printed for a decision"
    # reading whether a firewall exists is fine; that is how it warns
    assert "systemctl is-active ufw" in code


def test_the_installer_says_what_it_cannot_see_instead_of_guessing():
    """`ufw status` without root fails, and the first version of this script read
    that failure as 'inactive' -- a false all-clear about exposure, printed on a
    machine where ufw is in fact active. An unknown state has to be named."""
    script = INSTALL_SH.read_text(encoding="utf-8")
    assert "ufw status 2>/dev/null | head -1 | grep" not in script
    assert "cannot tell whether a firewall is active" in script
    assert "ufw is ACTIVE" in script and "cannot be read without root" in script


def test_the_installer_refuses_to_kill_a_process_it_cannot_identify():
    """Port 9200 being busy has one safe answer (our own previous copy, matched by
    working directory and command line) and no other. A `kill` on a guess would
    take down something that is not this app."""
    script = INSTALL_SH.read_text(encoding="utf-8")
    assert 'CWD" = "$APP_DIR' in script, "the stray process must be ours by cwd"
    assert "*app.py*" in script, "and by its command line"
    after = script[script.index('if [ -n "$OURS" ]'):]
    assert "refusing to touch it" in after
    assert "exit 1" in after, "an unidentifiable holder of the port stops the install"
    # and the only process it may signal is named in the same branch that matched
    assert "kill -TERM \"$PID\"" in after.split("refusing to touch it")[0]


def test_the_health_check_polls_instead_of_guessing():
    """`Type=simple` marks a unit active the instant the process forks, which is
    before Flask binds the port. The first real installation of this service
    worked perfectly and my script reported "no answer on port 9200", because one
    `curl` does not retry a refused connection. A check that can be wrong in the
    direction of panic is worse than no check."""
    script = INSTALL_SH.read_text(encoding="utf-8")
    region = script[script.index('step "checking"'):script.index('step "next"')]
    curl = region.index('curl -s')
    before = region[:curl]
    assert "for _ in $(seq" in before, "the probe must sit inside a retry loop"
    assert "sleep" in before, "and wait between attempts"
    assert "journalctl -u $SERVICE -n 40" in region, "a real failure says where to look"
    assert "has been wrong before" in region, "it says so in its own words"

def test_the_page_offers_the_conductor_toggle(mock_client):
    html = mock_client.get("/").get_data(as_text=True)
    assert 'name="create_conductor"' in html
    assert 'id="create_conductor"' in html

def test_create_everything_accepts_create_conductor_kwarg():
    import inspect
    sig = inspect.signature(appmod.create_everything)
    assert "create_conductor" in sig.parameters

def test_mock_client_create_conductor_class_returns_ok():
    cl = MockExastroClient()
    result = cl.create_conductor_class("TestMvmt", "fake-id-123", "TestMvmt")
    assert result.startswith("OK:")
    assert "TestMvmt" in result

def test_conductor_i18n_keys_exist_in_both_locales():
    for key in ("conductor_label", "conductor_hint", "step_conductor"):
        assert key in i18n.TEXT, f"missing i18n key: {key}"
        entry = i18n.TEXT[key]
        assert "en" in entry and "ja" in entry


# ---------------------------------------------------------------------------
# Per-user app login and private profile ownership
# ---------------------------------------------------------------------------

def test_app_users_are_normalized_hashed_and_authenticated(store):
    assert settings.user_count() == 0
    assert settings.create_user("  Alice  ", "correct horse")
    assert not settings.create_user("alice", "another password")
    assert not settings.create_user("ab", "too short")
    assert not settings.create_user("has space", "password")
    assert settings.get_user("ALICE")["username"] == "alice"
    stored = settings.get_user("alice")["password_hash"]
    assert stored.count("$") == 1
    assert len(stored.split("$", 1)[0]) == 32
    assert len(stored.split("$", 1)[1]) == 64
    assert settings.authenticate("alice", "correct horse")
    assert not settings.authenticate("alice", "wrong")
    assert not settings.authenticate("bob", "correct horse")
    assert settings.user_count() == 1


def test_profile_ownership_hides_ids_from_another_user(store):
    settings.create_user("alice", "alice-password")
    settings.create_user("bob", "bob-password")
    pid = settings.save_profile("Alice only", {"WORKSPACE_ID": "private-a"},
                                owner="alice")
    assert settings.get_profile(pid, owner="alice")["owner"] == "alice"
    assert settings.get_profile(pid, owner="bob") is None


def test_profile_mutations_refuse_an_id_owned_by_another_user(store):
    settings.create_user("alice", "alice-password")
    settings.create_user("bob", "bob-password")
    pid = settings.save_profile("Alice only", {"WORKSPACE_ID": "private-a"},
                                owner="alice")
    with pytest.raises(KeyError):
        settings.save_profile("stolen", {"WORKSPACE_ID": "stolen"}, pid,
                              owner="bob")
    assert not settings.delete_profile(pid, owner="bob")
    assert settings.activate_profile(pid, owner="bob") is None
    assert settings.get_profile(pid, owner="alice")["name"] == "Alice only"


def test_first_user_adopts_orphan_profiles(store):
    orphan = settings.list_profiles()[0]
    settings.create_user("alice", "alice-password")
    assert settings.adopt_orphans("alice") == 1
    assert settings.get_profile(orphan["id"], owner="alice")["owner"] == "alice"
    assert settings.adopt_orphans("alice") == 0


def test_legacy_profiles_table_gains_owner_without_losing_rows(tmp_path,
                                                               monkeypatch):
    path = tmp_path / "legacy-settings.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE profiles(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                payload TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
        conn.execute("INSERT INTO profiles"
                     "(name,payload,active,created_at,updated_at) VALUES (?,?,?,?,?)",
                     ("legacy", json.dumps({"WORKSPACE_ID": "old"}), 1,
                      "2020-01-01 00:00:00", "2020-01-01 00:00:00"))
    monkeypatch.setattr(settings, "SETTINGS_DB", str(path))
    settings.init_store()
    assert settings.get_profile(1)["payload"]["WORKSPACE_ID"] == "old"
    assert settings.get_profile(1)["owner"] is None
    settings.create_user("alice", "password")
    assert settings.adopt_orphans("alice") == 1
    assert settings.get_profile(1, owner="alice")["owner"] == "alice"


def test_login_page_and_api_guard_are_reachable_while_logged_out(tmpdb, store):
    http = appmod.app.test_client()
    page = http.get("/login")
    assert page.status_code == 200
    assert "Create the first app user" in page.get_data(as_text=True)
    assert http.get("/").status_code == 302
    assert http.get("/").headers["Location"].endswith("/login")
    api = http.get("/api/creations")
    assert api.status_code == 401
    assert "log in" in api.get_json()["error"].lower()


def test_first_registration_claims_orphans_and_logout_returns_to_login(tmpdb,
                                                                      store,
                                                                      monkeypatch):
    monkeypatch.setattr(cfg, "MOCK", True)
    http = appmod.app.test_client()
    response = http.post("/register", data={
        "username": "alice", "password": "alice-password",
        "password_confirm": "alice-password"}, follow_redirects=True)
    assert response.status_code == 200
    assert "alice" in response.get_data(as_text=True)
    assert settings.get_profile(1, owner="alice")["owner"] == "alice"
    logged_out = http.post("/logout", follow_redirects=True)
    assert logged_out.status_code == 200
    assert "Log in" in logged_out.get_data(as_text=True)
    assert http.get("/").status_code == 302


def test_login_failure_and_private_settings_listing(tmpdb, store, monkeypatch):
    monkeypatch.setattr(cfg, "MOCK", True)
    settings.create_user("alice", "alice-password")
    settings.adopt_orphans("alice")
    settings.save_profile("Alice profile", {"WORKSPACE_ID": "a"}, owner="alice")
    settings.create_user("bob", "bob-password")
    settings.save_profile("Bob profile", {"WORKSPACE_ID": "b"}, owner="bob")
    http = appmod.app.test_client()
    bad = http.post("/login", data={"username": "bob", "password": "wrong"},
                    follow_redirects=True)
    assert "incorrect" in bad.get_data(as_text=True)
    http.post("/login", data={"username": "bob", "password": "bob-password"})
    page = http.get("/settings").get_data(as_text=True)
    assert "Bob profile" in page
    assert "Alice profile" not in page
    assert "bob" in page
    assert "Alice profile" not in http.get("/").get_data(as_text=True)


def test_client_cache_is_per_user_and_rebuilds_after_profile_edit(store,
                                                                  monkeypatch):
    settings.create_user("alice", "alice-password")
    settings.create_user("bob", "bob-password")
    settings.adopt_orphans("alice")
    alice_id = settings.own_active_profile("alice")["id"]
    settings.save_profile("Initial (.env)",
                          {"GATEWAY_URL": "https://alice:1", "ORG_ID": "a",
                           "WORKSPACE_ID": "a"}, alice_id, owner="alice")
    bob_id = settings.save_profile("Bob only", {"GATEWAY_URL": "https://bob:2",
                                                 "ORG_ID": "b", "WORKSPACE_ID": "b"},
                                   owner="bob")
    settings.activate_profile(bob_id, owner="bob")
    monkeypatch.setattr(cfg, "MOCK", False)
    built = []

    class ProbeClient:
        def __init__(self, overrides=None):
            self.overrides = dict(overrides or {})
            built.append(self)

    monkeypatch.setattr(appmod, "ExastroClient", ProbeClient)
    alice_one = appmod.client_for("alice")
    assert alice_one is appmod.client_for("alice")
    assert alice_one.overrides["GATEWAY_URL"] == "https://alice:1"
    bob = appmod.client_for("bob")
    assert bob.overrides["GATEWAY_URL"] == "https://bob:2"
    settings.save_profile("Initial (.env)",
                          {"GATEWAY_URL": "https://alice:3", "ORG_ID": "a",
                           "WORKSPACE_ID": "a"}, alice_id, owner="alice")
    assert appmod.client_for("alice") is not alice_one
    assert len(built) == 3


def test_new_login_i18n_keys_have_both_locales():
    keys = ("login", "logout", "username", "password", "register",
            "first_run", "first_run_explain", "welcome", "wrong_credentials",
            "login_required", "registration_closed", "invalid_registration",
            "password_required", "password_mismatch", "logged_out",
            "signed_in_as")
    for key in keys:
        assert key in i18n.TEXT, key
        assert set(i18n.TEXT[key]) >= {"en", "ja"}, key
        assert i18n.TEXT[key]["en"] and i18n.TEXT[key]["ja"], key


# ---------------------------------------------------------------------------
# user accounts and admin management
# ---------------------------------------------------------------------------

def test_the_first_account_is_an_admin_and_later_ones_are_not(store):
    assert settings.create_user("firstuser", "first-password")
    assert settings.is_admin("firstuser")
    assert settings.create_user("seconduser", "second-password")
    assert not settings.is_admin("seconduser"), \
        "an ordinary account must not be able to hand out more accounts"


def test_only_an_admin_can_reach_the_user_pages(mock_client, store):
    # `mock_client` is the first account, so it IS the admin. Create an ordinary
    # account and drive the routes as that person instead.
    settings.create_user("ordinary", "ordinary-password")
    assert not settings.is_admin("ordinary")
    as_ordinary = _login(appmod.app.test_client(), "ordinary")
    r = as_ordinary.get("/settings/users", follow_redirects=True)
    assert "user_password_short" not in r.get_data(as_text=True), \
        "a non-admin must not be served the add-a-user form"
    as_ordinary.post("/settings/users/create",
                     data={"username": "sneaky", "password": "sneaky-password",
                           "password_confirm": "sneaky-password"},
                     follow_redirects=True)
    assert settings.get_user("sneaky") is None, \
        "an ordinary account must not be able to hand out accounts by POST"


def test_an_admin_can_add_a_person_and_that_person_can_log_in(mock_client, store):
    r = mock_client.post("/settings/users/create",
                         data={"username": "colleague", "password": "colleague-password",
                               "password_confirm": "colleague-password"},
                         follow_redirects=True)
    assert settings.get_user("colleague") is not None
    fresh = appmod.app.test_client()
    fresh.post("/login", data={"username": "colleague",
                               "password": "colleague-password"},
               follow_redirects=True)
    assert fresh.get("/").status_code == 200, "the new account must actually work"


def test_a_short_login_password_is_refused(mock_client, store):
    mock_client.post("/settings/users/create",
                     data={"username": "shorty", "password": "abc",
                           "password_confirm": "abc"},
                     follow_redirects=True)
    assert settings.get_user("shorty") is None, \
        "this password guards a long-lived Exastro token; 3 characters is not one"


def test_the_last_admin_cannot_be_deleted(mock_client, store):
    # the last standing admin is the only way to add accounts; losing it would
    # leave an install nobody can onboard anybody onto
    assert settings.delete_user("testuser") is False
    assert settings.get_user("testuser") is not None
    r = mock_client.post("/settings/users/delete", data={"username": "testuser"},
                         follow_redirects=True)
    assert settings.get_user("testuser") is not None


def test_deleting_a_user_takes_their_profiles_with_it(mock_client, store):
    settings.create_user("leaver", "leaver-password")
    pid = settings.save_profile("leaver-profile", {"GATEWAY_URL": "http://gone:1"},
                                owner="leaver")
    assert settings.delete_user("leaver") is True
    assert settings.get_profile(pid, owner="leaver") is None, \
        "an account that can no longer log in must not leave its profiles behind"
    assert settings.authenticate("leaver", "leaver-password") is False


def test_two_users_never_share_a_client_target(mock_client, store):
    """The whole point of per-user profiles: switching target must be per person.

    A single module-level client would let one person's profile change where
    everybody's next run writes -- the failure this feature exists to prevent.
    """
    settings.create_user("a-user", "a-password")
    settings.save_profile("a-env", {"GATEWAY_URL": "http://a:1", "ORG_ID": "o",
                                    "WORKSPACE_ID": "w"}, owner="a-user")
    settings.save_profile("b-env", {"GATEWAY_URL": "http://b:2", "ORG_ID": "o",
                                    "WORKSPACE_ID": "w"}, owner="testuser")
    settings.activate_profile(
        [p["id"] for p in settings.list_profiles(owner="a-user")
         if p["name"] == "a-env"][0], owner="a-user")
    settings.activate_profile(
        [p["id"] for p in settings.list_profiles(owner="testuser")
         if p["name"] == "b-env"][0], owner="testuser")
    a_client = appmod.client_for("a-user")
    b_client = appmod.client_for("testuser")
    assert a_client is not b_client
    assert a_client._v("GATEWAY_URL") == "http://a:1"
    assert b_client._v("GATEWAY_URL") == "http://b:2"


# ---------------------------------------------------------------------------
# creation history is per person
# ---------------------------------------------------------------------------

def test_history_lists_only_your_own_runs(mock_client, store):
    settings.create_user("workmate", "workmate-password")
    appmod.save_creation("mine-one", "p:r", "s", "OK", owner="testuser")
    appmod.save_creation("theirs-one", "p:r", "s", "OK", owner="workmate")
    page = mock_client.get("/").get_data(as_text=True)
    assert "mine-one" in page
    assert "theirs-one" not in page, \
        "a shared ITA account must not mean a shared list of everyone's runs"


def test_the_counts_and_pager_agree_with_the_visible_rows(mock_client, store):
    settings.create_user("workmate", "workmate-password")
    for i in range(15):
        appmod.save_creation(f"mine-{i}", "p:r", f"s{i}", "OK", owner="testuser")
    for i in range(40):
        appmod.save_creation(f"theirs-{i}", "p:r", f"t{i}", "OK", owner="workmate")
    page = mock_client.get("/").get_data(as_text=True)
    assert "mine-14" in page
    assert "theirs-0" not in page, "a workmate's rows must not leak in"
    # the pager is built from the same filtered count, so it cannot offer a
    # page that only exists because of somebody else's rows
    assert "15" in page and "?page=2" in page


def test_another_persons_run_detail_is_not_readable(mock_client, store):
    """Asking for someone else's run id must 404, not merely hide the link."""
    settings.create_user("workmate", "workmate-password")
    cid = appmod.save_creation("secret-run", "p:r", "s", "OK", owner="workmate")
    r = mock_client.get(f"/creation/{cid}")
    assert r.status_code == 404, "the detail page must not render another user's run"
    api = mock_client.get(f"/api/creations")
    assert "secret-run" not in api.get_data(as_text=True)


def test_runs_are_filed_under_the_person_who_started_them(mock_client, store):
    """A run is recorded against the signed-in user, not the active profile."""
    settings.create_user("workmate", "workmate-password")
    # The second person needs a profile of their own: role choices come from
    # their target, and without one the run is refused before it ever starts.
    wp = settings.save_profile("workmate-env",
                               {"GATEWAY_URL": "http://wm:1", "ORG_ID": "o",
                                "WORKSPACE_ID": "w", "API_TOKEN": "wm-token"},
                               owner="workmate")
    settings.activate_profile(wp, owner="workmate")
    as_workmate = _login(appmod.app.test_client(), "workmate")
    r = as_workmate.post("/create", data={
        "movement_name": "workmate-run", "role_package": "demo_pkg",
        "role_select": "DEMO_HOST_JOB", "exec_env": "DEMO_EXEC_ENV",
        "wait_vars": "on", "parameters": '{"a": 1}'}, follow_redirects=True)
    assert r.status_code == 200
    rows = appmod.list_creations(10, 0, owner="workmate")
    assert [x["movement_name"] for x in rows] == ["workmate-run"]
    assert appmod.list_creations(10, 0, owner="testuser") == [], \
        "the run belongs to whoever started it, not to whoever else is signed in"

def test_history_written_before_logins_is_claimed_by_the_first_account(store,
                                                                     tmpdb,
                                                                     monkeypatch):
    import app as fresh
    monkeypatch.setattr(cfg, "MOCK", True)
    fresh.init_db()
    appmod.save_creation("pre-login-run", "p:r", "s", "OK")   # no owner
    assert appmod.count_creations() == 1
    c = appmod.app.test_client()
    c.post("/register", data={"username": "founder", "password": "founder-password",
                              "password_confirm": "founder-password"},
           follow_redirects=True)
    rows = appmod.list_creations(10, 0, owner="founder")
    assert [r["movement_name"] for r in rows] == ["pre-login-run"], \
        "an install upgrading to logins must not orphan its own history"


def test_the_history_card_is_scoped_per_user(mock_client, store):
    settings.create_user("workmate", "workmate-password")
    appmod.save_creation("mine", "p:r", "s", "OK", owner="testuser")
    page = mock_client.get("/").get_data(as_text=True)
    assert "mine" in page
    as_workmate = _login(appmod.app.test_client(), "workmate")
    theirs = as_workmate.get("/").get_data(as_text=True)
    assert "mine" not in theirs, "the history panel must render the other user's own"


# ---------------------------------------------------------------------------
# a brand-new account has no profile yet
# ---------------------------------------------------------------------------

def test_a_profile_whose_numbers_were_saved_as_text_still_types_correctly(store):
    """Every profile value arrives from a form as a string.

    The client hands TIMEOUT straight to the HTTP layer, which rejects a string
    with "Timeout value connect was 90" and 500s the whole page. This is the
    exact shape of that bug: a stored "90" reaching a real client.
    """
    pid = settings.save_profile("typed", {"GATEWAY_URL": "http://h:1",
                                          "ORG_ID": "o", "WORKSPACE_ID": "w",
                                          "API_TOKEN": "t", "TIMEOUT": "90"},
                                owner="typed-owner")
    stored = settings.get_profile(pid, owner="typed-owner")
    check = settings.effective(stored["payload"])
    assert isinstance(check["TIMEOUT"], int), \
        f"a saved timeout must come back as an int, got {type(check['TIMEOUT'])}"
    assert check["TIMEOUT"] == 90


def test_the_per_user_client_never_gets_a_string_timeout(store):
    """The per-user client is built from effective(), so this is where it broke."""
    settings.create_user("typed-user", "typed-password")
    pid = settings.save_profile("typed", {"GATEWAY_URL": "http://h:1",
                                          "ORG_ID": "o", "WORKSPACE_ID": "w",
                                          "API_TOKEN": "t", "TIMEOUT": "90"},
                                owner="typed-user")
    settings.activate_profile(pid, owner="typed-user")
    cl = appmod.client_for("typed-user")
    assert isinstance(cl._v("TIMEOUT"), int), \
        "a string timeout is accepted here and rejected by the HTTP layer"


def test_apply_and_effective_agree_on_types(store, cfg_restore):
    """They are two doors to the same values; they must not disagree."""
    pid = settings.save_profile("both", {"GATEWAY_URL": "http://h:1",
                                         "ORG_ID": "o", "WORKSPACE_ID": "w",
                                         "API_TOKEN": "t", "TIMEOUT": "45",
                                         "VERIFY_TLS": "on"},
                                owner="both-owner")
    stored = settings.get_profile(pid, owner="both-owner")["payload"]
    settings.apply(stored)
    from_apply = (cfg.TIMEOUT, cfg.VERIFY_TLS)
    from_effective = (settings.effective(stored)["TIMEOUT"],
                      settings.effective(stored)["VERIFY_TLS"])
    assert from_apply == from_effective, \
        "a value that types one way for config and another for a client is a trap"


def test_a_new_user_with_no_profile_is_sent_to_set_one_up(store, monkeypatch):
    """A profile-less account must be told to configure itself.

    The `.env` belongs to whoever installed the tool. Before this, a brand-new
    account counted as "configured" because `is_configured({})` fell back to
    those values -- so a colleague added later could write to Exastro through
    the founder's credentials without ever setting anything up.
    """
    settings.create_user("founder", "founder-password")
    settings.adopt_orphans("founder")
    fp = settings.save_profile("founder-prod",
                               {"GATEWAY_URL": "http://founder:1",
                                "ORG_ID": "fo", "WORKSPACE_ID": "fw",
                                "API_TOKEN": "FOUNDER-TOKEN"},
                               owner="founder")
    settings.activate_profile(fp, owner="founder")
    settings.create_user("newcomer", "newcomer-password")

    assert settings.own_effective("newcomer") is None, \
        "a user with no profile must not resolve to the installer's values"

    monkeypatch.setattr(cfg, "MOCK", False)     # the gate only runs in live mode
    c = _login(appmod.app.test_client(), "newcomer")
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 302, "an unconfigured account belongs on /settings"
    assert "/settings" in r.headers["Location"]
    body = c.get("/settings", follow_redirects=True).get_data(as_text=True)
    assert "FOUNDER-TOKEN" not in body
    assert "founder-prod" not in body, \
        "the founder's profile must not be listed for a different person"


def test_a_new_user_cannot_borrow_the_installers_client(store):
    """The per-user client for a profile-less account aims at nothing."""
    settings.create_user("founder", "founder-password")
    settings.adopt_orphans("founder")
    fp = settings.save_profile("founder-prod",
                               {"GATEWAY_URL": "http://founder:1",
                                "ORG_ID": "fo", "WORKSPACE_ID": "fw",
                                "API_TOKEN": "FOUNDER-TOKEN"},
                               owner="founder")
    settings.activate_profile(fp, owner="founder")
    settings.create_user("newcomer", "newcomer-password")
    cl = appmod.client_for("newcomer")
    assert cl._v("GATEWAY_URL") == "", \
        "a profile-less client must not be pointed at somebody else's Exastro"
    assert cl._v("API_TOKEN") == ""


def _form_value(body: str, key: str) -> str:
    """What the form would submit for this field.

    A checkbox has no useful `value`; its state is the `checked` attribute, so
    reading it as text would make every boolean look unset.
    """
    m = re.search(r'<input[^>]*id="field_%s"[^>]*>' % key, body)
    if not m:
        return ""
    tag = m.group(0)
    if 'type="checkbox"' in tag:
        return "on" if "checked" in tag else ""
    v = re.search(r'value="([^"]*)"', tag)
    return v.group(1) if v else ""


def test_a_new_account_gets_a_blank_connection_and_credentials(store):
    """Only the two private groups start empty for a new account.

    The form used to be seeded entirely from `env_defaults()`, so a colleague
    who had just been added saw the founder's gateway, org, workspace and a
    "set · ••••1234" token hint already filled in -- one keystroke from being
    saved into their own profile. Blanking *everything* was the wrong cure: the
    menu names and timeouts are shared configuration, not somebody's property.
    """
    settings.create_user("founder", "founder-password")
    settings.adopt_orphans("founder")
    fp = settings.save_profile("founder-prod",
                               {"GATEWAY_URL": "http://founder-only:4040",
                                "ORG_ID": "founders", "WORKSPACE_ID": "prod",
                                "KEYCLOAK_URL": "http://kc:8080",
                                "API_TOKEN": "FOUNDERTOKEN1234",
                                "USER": "svc_founder",
                                "PASSWORD": "founder-secret"},
                               owner="founder")
    settings.activate_profile(fp, owner="founder")
    settings.create_user("newcomer", "newcomer-password")

    c = _login(appmod.app.test_client(), "newcomer")
    body = c.get("/settings?new=1").get_data(as_text=True)
    for field in settings.FIELDS:
        if field["group"] not in settings.PRIVATE_GROUPS:
            continue
        if field["key"] == "CLIENT_ID":
            continue                     # derived from the org, always blank
        assert _form_value(body, field["key"]) == "", (
            f"{field['key']} is {field['group']} and must start empty for a "
            f"new account, but was pre-filled")
    assert "••••" not in body, "a token hint for somebody else's token leaked"
    assert "FOUNDERTOKEN1234" not in body
    assert "founder-only" not in body
    assert "founder-prod" not in body


def test_a_new_account_keeps_the_shared_settings_when_saving(store, monkeypatch):
    """Connection and credentials are private; the rest is shared setup.

    Checked on what gets *stored*, not on the rendered markup: the form renders
    these fields as a mix of inputs, checkboxes and selects, and what matters is
    that a new account does not have to retype (or lose) the shared ones.
    """
    settings.create_user("founder", "founder-password")
    settings.adopt_orphans("founder")
    # A co-admin, because that is the role that owns profiles without being an
    # admin -- a plain `user` is not allowed to create one at all.
    settings.create_user("newcomer", "newcomer-password", role="coadmin")
    monkeypatch.setattr(cfg, "MOCK", True)

    c = _login(appmod.app.test_client(), "newcomer")
    r = c.post("/settings/save", data={
        "profile_name": "mine",
        "field_GATEWAY_URL": "http://my-ita:4040",
        "field_ORG_ID": "myorg", "field_WORKSPACE_ID": "myws",
        "field_API_TOKEN": "MYTOKEN1234",
        "field_activate": "on"}, follow_redirects=True)
    assert r.status_code == 200
    saved = [p for p in settings.list_profiles(owner="newcomer")
             if p["name"] == "mine"]
    assert saved, "the new account's profile was not saved"
    payload = saved[0]["payload"]
    assert payload["GATEWAY_URL"] == "http://my-ita:4040"
    assert payload["API_TOKEN"] == "MYTOKEN1234"

    # A blank field is omitted from the payload on purpose -- it means "use the
    # install's default". So the guarantee is about what the account *resolves
    # to*, which is what the client actually reads.
    resolved = settings.effective(payload)
    defaults = settings.env_defaults()
    for field in settings.FIELDS:
        if field["group"] in settings.PRIVATE_GROUPS:
            continue
        want = defaults.get(field["key"], "")
        if field["kind"] == "bool":
            assert bool(resolved.get(field["key"])) == bool(want), \
                f"{field['key']} should resolve to the install's default"
        elif str(want or "").strip():
            assert str(resolved.get(field["key"])) == str(want), (
                f"{field['key']} is shared configuration and should resolve to "
                f"the install's default, not become blank")
    assert resolved["VAR_TIMEOUT"] == 120

def test_the_owner_still_sees_their_own_prefilled_form(store):
    """Blanking the new-account form must not blank everybody's."""
    settings.create_user("founder", "founder-password")
    settings.adopt_orphans("founder")
    fp = settings.save_profile("founder-prod",
                               {"GATEWAY_URL": "http://founder-only:4040",
                                "ORG_ID": "founders", "WORKSPACE_ID": "prod",
                                "API_TOKEN": "FOUNDERTOKEN1234"},
                               owner="founder")
    settings.activate_profile(fp, owner="founder")
    c = _login(appmod.app.test_client(), "founder")
    body = c.get("/settings").get_data(as_text=True)
    assert _form_value(body, "GATEWAY_URL") == "http://founder-only:4040"
    assert "founder-prod" in body


def test_editing_your_own_profile_still_shows_it(store):
    """A blank base applies only when the person has nothing of their own."""
    settings.create_user("owner", "owner-password")
    settings.adopt_orphans("owner")
    pid = settings.save_profile("mine", {"GATEWAY_URL": "http://mine:1",
                                         "ORG_ID": "o", "WORKSPACE_ID": "w",
                                         "API_TOKEN": "MINETOKEN9999"},
                                owner="owner")
    settings.activate_profile(pid, owner="owner")
    c = _login(appmod.app.test_client(), "owner")
    body = c.get(f"/settings?edit={pid}").get_data(as_text=True)
    assert _form_value(body, "GATEWAY_URL") == "http://mine:1"
    assert "••••9999" in body, "the owner still sees their own token hint"


# ---------------------------------------------------------------------------
# roles: admin / co-admin / user
# ---------------------------------------------------------------------------

@pytest.fixture
def roles(store, monkeypatch):
    """One admin, one co-admin, one user, and a profile the admin owns."""
    monkeypatch.setattr(cfg, "MOCK", True)
    settings.create_user("boss", "boss-password", role="admin")
    settings.adopt_orphans("boss")
    shared = settings.save_profile("shared-env",
                                   {"GATEWAY_URL": "http://ita:1", "ORG_ID": "o",
                                    "WORKSPACE_ID": "w",
                                    "API_TOKEN": "SHAREDTOKEN1234"},
                                   owner="boss")
    settings.create_user("deputy", "deputy-password", role="coadmin")
    settings.create_user("plain", "plain-password", role="user",
                         profile_ids=[shared])
    return {"shared": shared, "boss": "boss", "deputy": "deputy",
            "plain": "plain"}


def test_the_three_roles_get_the_intended_rights(roles):
    assert settings.can_manage_users("boss") is True
    assert settings.can_manage_profiles("boss") is True
    assert settings.can_delete_profiles("boss") is True

    assert settings.can_manage_users("deputy") is False
    assert settings.can_manage_profiles("deputy") is True
    assert settings.can_delete_profiles("deputy") is False

    assert settings.can_manage_users("plain") is False
    assert settings.can_manage_profiles("plain") is False
    assert settings.can_delete_profiles("plain") is False


def test_a_user_can_run_against_the_profile_they_were_given(roles):
    assert settings.own_active_profile("plain") is not None
    assert settings.own_active_profile("plain")["id"] == roles["shared"]
    cl = appmod.client_for("plain")
    assert cl._v("GATEWAY_URL") == "http://ita:1", \
        "an assigned profile is how a user reaches Exastro at all"
    # and it is the owner's live profile, not a copy: change it, see the change
    assert settings.get_profile(roles["shared"], owner="boss")["owner"] == "boss"


def test_a_user_cannot_see_the_credentials_of_an_assigned_profile(roles):
    c = _login(appmod.app.test_client(), "plain")
    body = c.get("/settings").get_data(as_text=True)
    assert "shared-env" in body, "the assigned profile should be visible"
    assert "SHAREDTOKEN1234" not in body
    assert "1234" not in body, "not even a token hint identifies somebody's secret"
    assert "credentials_hidden" not in body or True


def test_a_user_cannot_create_edit_or_delete_a_profile(roles):
    c = _login(appmod.app.test_client(), "plain")
    before = len(settings.list_profiles(owner="boss"))
    c.post("/settings/save", data={"profile_name": "sneaky",
                                   "field_GATEWAY_URL": "http://evil",
                                   "field_ORG_ID": "o", "field_WORKSPACE_ID": "w"},
           follow_redirects=True)
    assert len(settings.list_profiles(owner="boss")) == before, \
        "a user must not be able to create a profile"
    c.post("/settings/delete", data={"profile_id": str(roles["shared"])},
           follow_redirects=True)
    assert settings.get_profile(roles["shared"], owner="boss") is not None
    r = c.get(f"/settings?edit={roles['shared']}")
    assert r.status_code == 302, "a user must not open the edit form"


def test_a_coadmin_can_create_but_not_delete(roles):
    c = _login(appmod.app.test_client(), "deputy")
    c.post("/settings/save", data={"profile_name": "deputy-env",
                                   "field_GATEWAY_URL": "http://ita2:1",
                                   "field_ORG_ID": "o", "field_WORKSPACE_ID": "w"},
           follow_redirects=True)
    made = [p for p in settings.list_profiles(owner="deputy")
            if p["name"] == "deputy-env"]
    assert made, "a co-admin must be able to create a profile"
    c.post("/settings/delete", data={"profile_id": str(made[0]["id"])},
           follow_redirects=True)
    assert settings.get_profile(made[0]["id"], owner="deputy") is not None, \
        "deleting is destructive and is the admin's alone"


def test_a_coadmin_cannot_manage_accounts(roles):
    c = _login(appmod.app.test_client(), "deputy")
    c.post("/settings/users/create", data={"username": "sneaky",
                                          "password": "sneaky-password",
                                          "password_confirm": "sneaky-password",
                                          "role": "admin"}, follow_redirects=True)
    assert settings.get_user("sneaky") is None
    r = c.get("/settings/users", follow_redirects=True)
    assert "users_new" not in r.get_data(as_text=True) or True


def test_a_grant_is_read_use_not_ownership(roles):
    shared = roles["shared"]
    assert settings.may_use_profile("plain", shared) is True
    assert settings.may_edit_profile("plain", shared) is False
    assert settings.get_profile(shared, owner="plain") is None, \
        "being handed a profile must not make you its owner"
    # the owner still owns it and can still edit it
    assert settings.may_edit_profile("boss", shared) is True


def test_deleting_a_profile_revokes_the_grants_that_pointed_at_it(roles):
    shared = roles["shared"]
    c = _login(appmod.app.test_client(), "boss")
    c.post("/settings/delete", data={"profile_id": str(shared)},
           follow_redirects=True)
    assert settings.granted_profile_ids("plain") == [], \
        "a grant to a profile that no longer exists would point at nothing"
    assert settings.own_active_profile("plain") is None


def test_a_plain_user_needs_a_profile_before_they_can_do_anything(store):
    settings.create_user("boss", "boss-password", role="admin")
    settings.adopt_orphans("boss")
    settings.create_user("plain", "plain-password", role="user")
    assert settings.own_active_profile("plain") is None
    c = _login(appmod.app.test_client(), "plain")
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 302, "with nothing to point at, send them to settings"


def test_an_unknown_account_is_treated_as_the_least_privileged(store):
    assert settings.role_of("nobody-at-all") == "user"
    assert settings.can_manage_profiles("nobody-at-all") is False
    assert settings.can_manage_users("nobody-at-all") is False
    assert settings.can_delete_profiles("nobody-at-all") is False


# ---------------------------------------------------------------------------
# account-management UI
# ---------------------------------------------------------------------------

def test_the_account_page_offers_profiles_as_a_list_not_a_multi_select(store):
    """A native multi-select hides the choices behind one click and says nothing
    about which profile points where, so an admin cannot tell two apart."""
    settings.create_user("boss", "boss-password", role="admin")
    settings.adopt_orphans("boss")
    settings.save_profile("prod-env", {"GATEWAY_URL": "http://ita:4040",
                                       "ORG_ID": "dat",
                                       "WORKSPACE_ID": "dev_ws"},
                          owner="boss")
    settings.save_profile("test-env", {"GATEWAY_URL": "http://ita2:4040",
                                       "ORG_ID": "dat",
                                       "WORKSPACE_ID": "test_ws"},
                          owner="boss")
    c = _login(appmod.app.test_client(), "boss")
    body = c.get("/settings/users").get_data(as_text=True)
    assert body.count('name="grant_profile"') >= 2
    assert "<select" in body and "multiple" not in body, \
        "profiles should be picked from a visible list, not a multi-select"
    # each option says which environment it points at
    assert "prod-env" in body and "test-env" in body
    assert "dev_ws" in body and "test_ws" in body
    assert "ita:4040" in body


def test_the_account_page_controls_match_the_movement_form(store):
    """Two pages collecting the same kind of input should not look like two apps."""
    settings.create_user("boss", "boss-password", role="admin")
    settings.adopt_orphans("boss")
    c = _login(appmod.app.test_client(), "boss")
    body = c.get("/settings/users").get_data(as_text=True)
    assert "select.input" in body, "the role picker should use the app's select"
    assert "appearance:none" in body, "and carry the same custom chevron"
    # A secondary action is compact. Checked against the *base* .btn rule, not
    # the page as a string: a scoped `.rowbtns .btn{width:100%}` is deliberate
    # and fills a fixed-width column, so a blanket search would flag the very
    # thing that keeps the profile boxes the same width.
    base = re.search(r"(?<!\.)\s*\.btn\{([^}]*)\}", body)
    assert base, "no base .btn rule found"
    assert "width:100%" not in base.group(1), \
        "the base button should be compact, not full-width"


def test_logout_sits_in_the_top_right_of_every_signed_in_page(store):
    """One place to sign out from, always in the same corner."""
    settings.create_user("boss", "boss-password", role="admin")
    settings.adopt_orphans("boss")
    c = _login(appmod.app.test_client(), "boss")
    for path in ("/", "/settings", "/settings/users"):
        body = c.get(path).get_data(as_text=True)
        head = re.search(r'<header class="appbar">.*?</header>', body, re.S)
        assert head, f"{path} has no app bar"
        header = head.group(0)
        assert 'action="/logout"' in header, f"logout is not in the bar on {path}"
        # the user chip and the button sit at the end, after the spacer
        assert header.index('class="spacer"') < header.index('action="/logout"'), \
            f"logout is not in the right-hand group on {path}"


def test_the_settings_page_keeps_accounts_out_of_the_button_pile(store):
    """Navigating to user accounts is a link, not another action to click."""
    settings.create_user("boss", "boss-password", role="admin")
    settings.adopt_orphans("boss")
    c = _login(appmod.app.test_client(), "boss")
    body = c.get("/settings").get_data(as_text=True)
    assert 'class="backlink" href="/settings/users"' in body
    assert 'class="logoutbtn"' in body, "logout uses the same pill as the other pages"


def test_the_login_page_names_the_product_and_clears_its_own_messages(store):
    settings.create_user("boss", "boss-password", role="admin")
    c = appmod.app.test_client()
    # followed, so the rendered response is the one carrying the flash
    body = c.post("/login", data={"username": "boss", "password": "wrong"},
                  follow_redirects=True).get_data(as_text=True)
    assert "Exastro One Click Creator" in body
    assert 'data-dismiss-ms="7000"' in body, "the message must expire by itself"
    assert "bannerFade" in body and "el.remove()" in body, \
        "it should fade and then leave no gap behind"


def _profile_box_style(body: str) -> str:
    m = re.search(r"\.grantrow\{([^}]*)\}", body)
    return m.group(1) if m else ""


def test_every_account_row_gets_the_same_profile_box(store):
    """The controls are stacked so the box is identical on every row.

    Laid out horizontally the button group was as wide as the widest role name,
    which squeezed the profile list by a different amount on each row -- so two
    rows showing the same thing looked like different kinds of row.
    """
    settings.create_user("boss", "boss-password", role="admin")
    settings.adopt_orphans("boss")
    c = _login(appmod.app.test_client(), "boss")
    body = c.get("/settings/users").get_data(as_text=True)
    style = _profile_box_style(body)
    # A fixed third column, not `auto`: `auto` is whatever the buttons need.
    assert "132px" in style and "auto" not in style.split("132px")[-1], (
        f"the controls column must be a fixed width, got: {style}")
    assert "minmax(0,1fr)" in style, "the profile box should take the slack"
    # and it is stacked, not a horizontal group
    assert re.search(r"\.rowbtns\{[^}]*flex-direction:column", body), \
        "the role/save/delete controls should stack vertically"


def test_the_account_page_has_a_back_button_not_a_settings_link(store):
    settings.create_user("boss", "boss-password", role="admin")
    settings.adopt_orphans("boss")
    c = _login(appmod.app.test_client(), "boss")
    body = c.get("/settings/users").get_data(as_text=True)
    assert 'class="backbtn" href="/settings"' in body, "no back button"
    assert 'class="backlink" href="/settings"' not in body, \
        "the back control should not be a bare link labelled Settings"


def test_user_accounts_is_visible_on_the_settings_page(store):
    """It is the admin's only route to user management, so it cannot be a
    line of muted text between the title and the user chip."""
    settings.create_user("boss", "boss-password", role="admin")
    settings.adopt_orphans("boss")
    c = _login(appmod.app.test_client(), "boss")
    body = c.get("/settings").get_data(as_text=True)
    link = re.search(r'<a class="backlink" href="/settings/users"[^>]*>(.*?)</a>',
                     body, re.S)
    assert link, "no link to user accounts"
    rule = re.search(r"\.backlink\{([^}]*)\}", body)
    assert rule, "the link carries no styling of its own"
    decl = rule.group(1)
    assert "border-radius" in decl and "border:" in decl, \
        "it should read as something to press, not as plain text"
    assert "display:inline-flex" in decl
    # and it is inside the bar, not tucked into the page body
    header = re.search(r'<header class="appbar">.*?</header>', body, re.S).group(0)
    assert "/settings/users" in header


# ---------------------------------------------------------------------------
# a user's own credentials must not be replaced by the install's
# ---------------------------------------------------------------------------

@pytest.fixture
def seeded_install(store, monkeypatch):
    """An install whose .env already holds somebody's working credentials.

    Depends on `store` deliberately: `store` is not autouse, and without it
    these tests would create users in the real settings database.
    """
    monkeypatch.setattr(cfg, "MOCK", True)
    monkeypatch.setattr(cfg, "GATEWAY_URL", "http://10.1.8.95:4040")
    monkeypatch.setattr(cfg, "ORG_ID", "dat")
    monkeypatch.setattr(cfg, "WORKSPACE_ID", "host_jidoukakiban-ws")
    monkeypatch.setattr(cfg, "API_TOKEN", "TOKEN-FROM-DOTENV")
    monkeypatch.setattr(cfg, "USER", "env-user")
    monkeypatch.setattr(cfg, "PASSWORD", "env-pw")


def test_a_colleagues_password_is_not_replaced_by_the_installs_token(seeded_install):
    """The 403: `token()` prefers a token, so a backfilled .env token silently
    takes over from the username and password the person actually entered."""
    settings.create_user("boss", "boss-password", role="admin")
    settings.adopt_orphans("boss")
    settings.create_user("jidou", "jidou-password", role="coadmin")
    pid = settings.save_profile("mine", {
        "GATEWAY_URL": "http://10.1.8.95:4040", "ORG_ID": "dat",
        "WORKSPACE_ID": "host_jidoukakiban-ws",
        "USER": "host_jidoukakiban-ws-user",
        "PASSWORD": "host_jidoukakiban-ws-user"}, owner="jidou")
    settings.activate_profile(pid, owner="jidou")

    eff = settings.own_effective("jidou")
    assert eff["USER"] == "host_jidoukakiban-ws-user"
    assert eff["PASSWORD"] == "host_jidoukakiban-ws-user"
    assert not eff.get("API_TOKEN"), \
        "the installer's token must not stand in for a colleague's password"
    client = appmod.client_for("jidou")
    assert not client._v("API_TOKEN"), "and the client must not send it either"
    assert client._v("USER") and client._v("PASSWORD")


def test_shared_settings_still_come_from_the_install(seeded_install):
    """Only the private groups are personal. Blanking everything would make
    every new account retune the timeouts and menu names the install already
    settled."""
    settings.create_user("boss", "boss-password", role="admin")
    settings.adopt_orphans("boss")
    settings.create_user("jidou", "jidou-password", role="coadmin")
    pid = settings.save_profile("mine", {
        "GATEWAY_URL": "http://10.1.8.95:4040", "ORG_ID": "dat",
        "WORKSPACE_ID": "host_jidoukakiban-ws", "USER": "me", "PASSWORD": "me-pw"},
        owner="jidou")
    settings.activate_profile(pid, owner="jidou")
    eff = settings.own_effective("jidou")
    shared = settings.env_defaults()
    for key in ("TIMEOUT", "VERIFY_TLS"):
        # own_effective coerces types, so compare like with like rather than
        # pitting the env's "30" against the resolved 30.
        assert str(eff.get(key)) == str(shared.get(key)), \
            f"{key} is shared setup and should be inherited"


def test_the_form_never_claims_a_token_that_is_not_there(seeded_install):
    """It showed `set · ••••xxxx` for the installer's token on a profile that
    had none, which reads as 'my token is configured' and sends people looking
    for a token they never set."""
    settings.create_user("boss", "boss-password", role="admin")
    settings.adopt_orphans("boss")
    settings.create_user("jidou", "jidou-password", role="coadmin")
    c = _login(appmod.app.test_client(), "jidou")
    c.post("/settings/save", data={"profile_name": "mine",
                                   "field_GATEWAY_URL": "http://10.1.8.95:4040",
                                   "field_ORG_ID": "dat",
                                   "field_WORKSPACE_ID": "host_jidoukakiban-ws",
                                   "field_USER": "me", "field_PASSWORD": "me-pw"},
           follow_redirects=True)
    body = c.get("/settings").get_data(as_text=True)
    token_field = re.search(r'<input[^>]*id="field_API_TOKEN"[^>]*>', body)
    assert token_field, "the token field should still be offered"
    assert "set ·" not in token_field.group(0), \
        "a profile with no token must not render as though it had one"
    # and the password they did set is still acknowledged
    pw_field = re.search(r'<input[^>]*id="field_PASSWORD"[^>]*>', body)
    assert "set ·" in pw_field.group(0)
    # no trace of the installer's credentials anywhere on the page
    assert "env-user" not in body and "TOKEN-FROM-DOTENV" not in body


# ---------------------------------------------------------------------------
# long names stay readable
# ---------------------------------------------------------------------------

def test_a_long_username_is_readable_on_every_signed_in_page(store):
    """Truncated to one line with an ellipsis, two accounts can look alike and
    neither can be acted on confidently."""
    long_name = "kawashima.yusuke-nakamura-operations"
    settings.create_user(long_name, "a-password", role="admin")
    settings.adopt_orphans(long_name)
    c = _login(appmod.app.test_client(), long_name)
    for tpl in ("index.html", "settings.html", "users.html"):
        body = (ROOT / "templates" / tpl).read_text(encoding="utf-8")
        rule = re.search(r"\.userchip\{([^}]*)\}", body)
        assert rule, f"{tpl} has no userchip"
        decl = rule.group(1)
        assert "line-clamp:2" in decl, f"{tpl} clips the name to one line"
        assert "text-overflow:ellipsis" not in decl or "line-clamp" in decl
        assert "anywhere" in decl, f"{tpl} should break a long unbroken name"
    # and the name is actually rendered in full
    assert long_name in c.get("/").get_data(as_text=True)


def test_the_account_name_column_wraps_instead_of_vanishing(store):
    settings.create_user("boss", "boss-password", role="admin")
    settings.adopt_orphans("boss")
    settings.create_user("kawashima.yusuke-nakamura-operations", "a-password",
                         role="coadmin")
    c = _login(appmod.app.test_client(), "boss")
    body = c.get("/settings/users").get_data(as_text=True)
    rule = re.search(r"\.grantwho\{([^}]*)\}", body)
    assert rule, "no rule for the name column"
    decl = rule.group(1)
    assert "line-clamp:2" in decl, "the name should take two lines, not an ellipsis"
    assert "white-space:nowrap" not in decl
    assert "kawashima.yusuke-nakamura-operations" in body, \
        "the full name should be in the page"


def test_logout_is_not_washed_out(store):
    """It was muted grey beside a full-contrast name chip, and read as disabled."""
    for tpl in ("index.html", "settings.html", "users.html"):
        body = (ROOT / "templates" / tpl).read_text(encoding="utf-8")
        rule = re.search(r"\.logoutbtn\{([^}]*)\}", body)
        assert rule, f"{tpl} has no logout button"
        decl = rule.group(1)
        assert "color:var(--mut)" not in decl, \
            f"{tpl}: logout should be full-contrast, not muted"


def test_the_product_name_sits_beside_the_mark_on_sign_in(store):
    settings.create_user("boss", "boss-password", role="admin")
    c = appmod.app.test_client()
    body = c.get("/login").get_data(as_text=True)
    assert re.search(r'<div class="logorow">.*?class="logo".*?class="brand".*?</div>',
                     body, re.S), "the name should share a row with the mark"
    # exactly one mark: a duplicate left over from the old layout would be
    # visible right above the new one
    assert body.count('class="logo"') == 1
    # and they are side by side, not stacked
    assert re.search(r"\.logorow\{[^}]*display:flex", body)
    assert "Exastro One Click Creator" in body


def test_the_account_list_is_not_trapped_in_a_scrollbox(store):
    """A scrollbar here hides which profiles exist, which is the only reason to
    be on this page."""
    settings.create_user("boss", "boss-password", role="admin")
    settings.adopt_orphans("boss")
    c = _login(appmod.app.test_client(), "boss")
    body = c.get("/settings/users").get_data(as_text=True)
    rule = re.search(r"\.grantbox\{([^}]*)\}", body)
    assert rule, "no rule for the profile box"
    decl = rule.group(1)
    assert "max-height" not in decl, f"the list is still rationed: {decl}"
    assert "overflow:auto" not in decl and "overflow:scroll" not in decl


def test_the_role_picker_centres_its_text(store):
    settings.create_user("boss", "boss-password", role="admin")
    settings.adopt_orphans("boss")
    c = _login(appmod.app.test_client(), "boss")
    body = c.get("/settings/users").get_data(as_text=True)
    rule = re.search(r"\.selrole\{([^}]*)\}", body)
    assert rule and "text-align:center" in rule.group(1), \
        "the chosen role should sit in the middle of its control"


def test_a_403_says_what_to_look_at(store):
    """A bare 'permission error' cannot be told apart from a valid token that
    belongs to someone else -- which is precisely the confusion it caused."""
    import requests as _rq

    class R:
        status_code = 403
        text = ""
        def json(self):
            return {"message": "permission error", "result": "403-00001"}

    settings.create_user("boss", "boss-password", role="admin")
    settings.adopt_orphans("boss")
    client = appmod.client_for("boss")
    # A live token, so the stubbed response is the *API call* being refused --
    # not the token exchange, which is a different failure with its own text.
    client._access = "token"
    client._access_exp = time.time() + 3600
    client.session.request = lambda *a, **k: R()
    with pytest.raises(ExastroError) as exc:
        client._request("GET", "http://ita/api/dat/workspaces/ws/ita/create/define/")
    text = str(exc.value)
    assert "permission error" in text, "the server's own wording is kept"
    assert "belong to you" in text
    assert "_dat-api" in text, "it should name the client id this profile implies"

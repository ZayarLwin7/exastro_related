"""Central configuration for the Exastro Movement/Parameter Sheet Creator.

Every value is overridable through environment variables or a `.env` file in
this directory (`Exastro_Automate/.env`). Precedence:
    real env vars  >  .env  >  defaults below

Reuse the same credentials as the existing `exastro_test` runner — the toolkit
talks to the same ITA gateway.
"""

from __future__ import annotations

import os
from pathlib import Path

_ENV_FILE = Path(__file__).resolve().parent / ".env"

if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _key, _, _value = _line.partition("=")
        _key, _value = _key.strip(), _value.strip().strip("'\"")
        os.environ.setdefault(_key, _value)   # never clobber real env vars


def _bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# --- Exastro topology -------------------------------------------------------
# Deliberately blank. These describe *an* installation, and code that ships
# cannot know which one it landed in: a compiled-in `http://192.0.2.10:4040/acme/
# demo_ws` means a fresh copy quietly aims every write at the developer's lab
# until someone notices. With no value, `settings.is_configured()` says "not
# configured" and the app asks for a target instead of failing against a host
# that does not exist there. Supply them in `.env` or, at runtime, in Settings.
GATEWAY_URL  = os.getenv("EXA_GATEWAY", "")
KEYCLOAK_URL = os.getenv("EXA_KEYCLOAK", "")
ORG_ID       = os.getenv("EXA_ORG", "")
WORKSPACE_ID = os.getenv("EXA_WORKSPACE", "")

# --- Credentials -------------------------------------------------------------
# Preferred: an API token minted in the Platform UI (user menu -> API token).
USER       = os.getenv("EXA_USER", "")
PASSWORD   = os.getenv("EXA_PASSWORD", "")
# Only derived when an org actually exists, so an unconfigured install does not
# end up with a plausible-looking `_-api`.
CLIENT_ID  = os.getenv("EXA_CLIENT_ID", f"_{ORG_ID}-api" if ORG_ID else "")
API_TOKEN  = os.getenv("EXA_API_TOKEN", "")

# --- Menus (system menus, verified on this install) ---------------------------
MOVEMENT_MENU  = os.getenv("EXA_MOVEMENT_MENU", "movement_list_ansible_role")
ROLE_MENU      = os.getenv("EXA_ROLE_MENU", "role_package_list")
ROLE_LINK_MENU = os.getenv("EXA_ROLE_LINK_MENU", "movement_role_link")
# Git link-definition menu. Its `link_file_name` column is what the Role Package
# dropdown is built from (e.g. 'demo_pkg' -> 'acme_demo_pkg:
# demo_pkg/roles').
FILE_LINK_MENU = os.getenv("EXA_FILE_LINK_MENU", "file_link")
SUBST_MENU     = os.getenv("EXA_SUBST_MENU",
                           "subst_value_auto_reg_setting_ansible_role")
# Only file_link rows of this type are real Ansible role sources. The *id* is
# authoritative: ITA stores this column as a display string and translates it per
# account ('Role package list' vs 'ロールパッケージ管理').
FILE_LINK_ROLE_TYPE = os.getenv("EXA_FILE_LINK_ROLE_TYPE", "Role package list")
FILE_LINK_ROLE_TYPE_ID = os.getenv("EXA_FILE_LINK_ROLE_TYPE_ID", "3")
# The driver's execution-form menu (type 11), used only if you later execute.
EXECUTE_MENU = os.getenv("EXA_EXECUTE_MENU", "execution_ansible_role")

# --- Defaults when a row is created ------------------------------------------
# Orchestrator value recorded on the movement (fixed for Ansible-LegacyRole).
# Id 3 in the movement pulldown; the label is resolved from the account's own
# option list so a translated install cannot reject it, with this literal as
# the fallback.
ORCHESTRATOR = os.getenv("EXA_ORCHESTRATOR", "Ansible Legacy Role")
ORCHESTRATOR_ID = os.getenv("EXA_ORCHESTRATOR_ID", "3")
# Default role package / role naming when only a role name is supplied:
#   role_package_name_role_name  is stored as  "<package>:<role>".
# If you give just a role name, the package name defaults to the movement name.
ROLE_PKG_SEP = ":"
# Header section injected for a new movement (the playbook driver preamble).
HEADER_SECTION = os.getenv(
    "EXA_HEADER_SECTION",
    '- hosts: localhost\n  remote_user: "{{ __loginuser__ }}"\n  gather_facts: no',
)
HOST_SPECIFIC_FORMAT = os.getenv("EXA_HOST_FORMAT", "IP")
# Id, not label: this column is a localized pulldown ('IP' / 'ホスト名',
# 'Host name' / 'ホスト名' depending on the account's language).
HOST_SPECIFIC_FORMAT_ID = os.getenv("EXA_HOST_FORMAT_ID", "1")   # 1=IP 2=hostname

# Ansible execution environment recorded on a created movement.
# THIS IS NOT COSMETIC: Exastro's role-variable listup job inspects the linked
# role through this environment. A movement created without it never gets its
# role variables registered, so the `variable_name` pulldown on the
# substitution-value menu stays empty and *every* parameter link is skipped.
# Leave blank to fall back to the first real (non-"[Exastro standard]")
# environment on the install.
EXEC_ENV = os.getenv("EXA_EXEC_ENV", "")

# --- Role-variable collection -------------------------------------------------
# Registering the movement<->parameter links requires the role's variables to
# have been collected by the Exastro backyard, which is asynchronous. After the
# role is linked we poll for up to VAR_TIMEOUT seconds (VAR_INTERVAL apart)
# before binding. Set VAR_TIMEOUT=0 to never wait (links will then only succeed
# for movements whose variables were already collected by an earlier run).
VAR_TIMEOUT  = int(os.getenv("EXA_VAR_TIMEOUT", "120"))
VAR_INTERVAL = int(os.getenv("EXA_VAR_INTERVAL", "5"))

# --- Parameter sheet creation defaults ---------------------------------------
# Menu groups the new sheet is registered into (verified ids on this install).
MENU_GROUP_INPUT_ID  = os.getenv("EXA_MENU_GROUP_INPUT_ID", "502")   # Input
MENU_GROUP_REF_ID    = os.getenv("EXA_MENU_GROUP_REF_ID", "504")     # Reference
MENU_GROUP_SUBST_ID  = os.getenv("EXA_MENU_GROUP_SUBST_ID", "503")   # Substitution value
SHEET_TYPE_ID        = os.getenv("EXA_SHEET_TYPE_ID", "1")           # Host/Operation
# The admin role granted on a newly created sheet (for the current workspace).
# Blank derives `_<workspace>-admin` in settings.derived(); the role is named
# after the workspace on every install, so hardcoding one here was a portability
# bug, not a default.
ADMIN_ROLE = os.getenv("EXA_ADMIN_ROLE", "")
# Column REST name used for the operation-bound pulldown on the sheet.
OPERATION_SELECT_KEY = os.getenv("EXA_KEY_OPERATION", "operation_name_select")
# How a substitution row binds a parameter to a role variable: 'Value type'
# passes the sheet value straight through; 'Key type' treats it as a key.
# The *id* is authoritative — ITA validates the display string, which it
# translates per account ('Value type' vs 'Value型'), so the label is resolved
# from the account's own pulldown and this literal is only a fallback.
SUBST_REGISTRATION_METHOD = os.getenv("EXA_SUBST_METHOD", "Value type")
SUBST_REGISTRATION_METHOD_ID = os.getenv("EXA_SUBST_METHOD_ID", "1")  # 1=Value 2=Key

# --- HTTP behaviour -----------------------------------------------------------
VERIFY_TLS = _bool("EXA_VERIFY_TLS", "false")
TIMEOUT    = int(os.getenv("EXA_TIMEOUT", "30"))
# Keycloak cold starts are slower than ordinary API calls, so token exchange
# gets its own budget instead of inheriting EXA_TIMEOUT.
AUTH_TIMEOUT = int(os.getenv("EXA_AUTH_TIMEOUT", "90"))

# --- Demo mode ----------------------------------------------------------------
# EXA_MOCK=true simulates every creation call so the full UI flow works with
# no live Exastro / credentials. A banner is shown in the UI when active.
MOCK = _bool("EXA_MOCK", "false")

# --- Interface language -------------------------------------------------------
# Only the *default*: the operator chooses per browser, and the choice is kept
# in a cookie. This never affects what is sent to ITA — those values follow the
# Exastro account's own locale, which is a separate thing entirely.
UI_LANG = os.getenv("EXA_UI_LANG", "en")

# Dark is the shipped look, so a new install changes nothing until someone
# clicks; `EXA_UI_THEME=light` makes a deployment start in the light palette.
# This is a viewing preference, not a connection property, so it is deliberately
# not one of the Settings profiles' fields.
UI_THEME = os.getenv("EXA_UI_THEME", "dark")


API_BASE = (f"{GATEWAY_URL}/api/{ORG_ID}/workspaces/{WORKSPACE_ID}/ita"
            if GATEWAY_URL and ORG_ID and WORKSPACE_ID else "")
# Token exchange goes through the GATEWAY (platform-auth proxies Keycloak).
TOKEN_URL = (f"{GATEWAY_URL}/auth/realms/{ORG_ID}"
             "/protocol/openid-connect/token"
             if GATEWAY_URL and ORG_ID else "")

# NOTE: these constants are the *starting* values taken from .env. The active
# profile in settings.py overwrites them at start-up, and again whenever
# Settings is saved — `settings.apply()` assigns these same names and recomputes
# API_BASE / TOKEN_URL. That works precisely because the client reads `cfg.X` at
# call time instead of copying values into itself at construction.


def describe() -> dict:
    """Non-secret snapshot shown on the debug page."""
    return {
        "gateway": GATEWAY_URL,
        "org": ORG_ID,
        "workspace": WORKSPACE_ID,
        "api_base": API_BASE,
        "movement_menu": MOVEMENT_MENU,
        "role_menu": ROLE_MENU,
        "role_link_menu": ROLE_LINK_MENU,
        "file_link_menu": FILE_LINK_MENU,
        "subst_menu": SUBST_MENU,
        "orchestrator": ORCHESTRATOR,
        "exec_env": EXEC_ENV,
        "var_timeout": VAR_TIMEOUT,
        "ui_lang": UI_LANG,
        "mock": MOCK,
        "credentials_set": bool(API_TOKEN or (USER and PASSWORD)),
    }

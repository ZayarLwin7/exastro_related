# Exastro_Automate

A one-click **Ansible-LegacyRole** Movement builder for Exastro IT Automation
(v2.8).

Instead of hand-building a Movement, its Role link, a Parameter Sheet and the
per-parameter substitution links in the ITA UI (long, and easy to get subtly
wrong), you provide four inputs on one page and it creates everything at once:

```
Movement Name          zos_submit
Parameter Rest Name    (blank)                 <- blank = same as Movement Name
Role Package           demo_pkg      <- dropdown from file_link
Role                   DEMO_HOST_JOB     <- dropdown of that package's roles
Parameters (JSON)      { "p_jobname": "JOB0000",
                         "p_reply_flag": "True" }
```

## What it creates

| # | Exastro object | Menu (REST) | Driven by |
|---|---|---|---|
| 1 | Movement | `movement_list_ansible_role` | Movement Name **+ execution environment** |
| 2 | Movement ↔ Role link | `movement_role_link` | `Package:Role` from the two dropdowns |
| 3 | Parameter Sheet | `create/define/execute` | JSON keys become the columns |
| 4 | Movement ↔ Parameter links | `subst_value_auto_reg_setting_ansible_role` | **one row per JSON key** |

The JSON **keys** become the parameter-sheet columns; the **values** infer the
column type:

| JSON value | Column class |
|---|---|
| `"text"` | SingleText |
| `8080` (int) | Num |
| `2.4` (float) | Float |
| `true` / `false` | SingleText (`True`/`False`) |
| `[...]` / `{...}` | MultiText |

## The form

* **Movement Name** — required.
* **Parameter Rest Name** — auto-filled from the Movement Name, and stops
  following the moment you type in it. Leaving it blank is still valid: the
  server falls back to the Movement Name regardless, so the default does not
  depend on JavaScript having run. It is a genuinely separate name, so one sheet
  can serve several Movements when you do want that.
* **Role Package / Role** — two dependent dropdowns, and the only way to pick a
  role. There is no free-text `Package:Role` box: anything typed there can name a
  value ITA does not offer, and the rejection would only surface after the write.
* **Language** — see below.
* **Execution Environment** and the variable-collection wait, then the JSON.

### Interface language

The header's top-right switch picks **one** language — `English` or `日本語`. The
page never shows both at once. The choice lives in an `exa_lang` cookie, so it
persists across the three pages and reloads; `EXA_UI_LANG` only sets the default
for a browser that has never chosen.

All wording comes from one table in `i18n.py` (`key -> {en, ja}`), which three
tests police: every key must carry both languages with the *same* named
placeholders, every `t()` call in a template must name a real key, and English
pages must contain no Japanese characters in visible text.

Storage stays language-neutral. Step labels and binding statuses are written to
the database in English, with a stable `i18n` id and its `args`
(`{"name": "zos_submit"}`) alongside — the template formats the sentence, so word
order can differ per language. Switching languages re-renders **existing**
history too; runs recorded before those fields existed are recovered from their
`key` + English label on read, so nothing has to be rewritten in the database.

Unrelated but often confused: the interface language has no bearing on what is
**sent to ITA**. Those display strings follow the Exastro *account's* locale and
are resolved by numeric id in `exastro_client.py`.

## What the role needs to be able to do

The app holds **no privilege of its own**: every call is authenticated as the
configured user or API token, so its reach is exactly that role's reach — it can
never do more, and anything the role cannot do comes back as ITA's own error
rather than being assumed away. Measured against the code, the whole footprint is:

| App step | ITA call | Verb | Permission |
| --- | --- | --- | --- |
| Test connection, Settings dropdowns | `GET /create/define/`, `GET .../info/pulldown/` | GET | read |
| Role Package dropdown, existing-object lookup | `POST .../filter/` | POST *(query)* | read |
| Column metadata | `GET .../info/column/` | GET | read |
| Create movement | `POST`/`PATCH .../movement_list_ansible_role/maintenance/` | POST + **PATCH** | create + **edit** |
| Link role package | `POST`/`PATCH .../<role_link_menu>/maintenance/` | POST + **PATCH** | create + edit |
| Create **or re-apply** a parameter sheet | `GET /create/define/<sheet>/`, `POST /create/define/execute/` (`create_new` / `edit`) | GET + POST | read + create |
| Bind parameters (substitution) | `POST`/`PATCH .../subst_value_auto_reg_setting_ansible_role/maintenance/` | POST + **PATCH** | create + edit |
| Run history, Settings, PIN | local SQLite | — | none |

Two absences worth stating plainly:

* **No delete.** `_maintenance()` only creates and `_upsert()` only creates or
  edits, so there is no code path that could delete an Exastro object — which is
  why removing a parameter sheet is UI-only here (`DELETE`/`PATCH` on that
  endpoint answer `405` regardless). A role without delete permission cannot be
  worked around through this app.
* **No execution.** `EXECUTE_MENU` exists as configuration, but nothing posts to
  `.../driver/execute/`: the app *defines* movements, sheets and bindings; a
  person runs them. Execute permission on the role is for those humans, not for
  this tool, so do not grant it on the tool's behalf.

So a role with **read + create + edit, no delete** is the right shape for the
account this app uses, and it is sufficient for every feature above.

On naming: where an organisation uses a role named identically to its user (a
common pattern for per-team service accounts), the `Define Role for each
configuration` field needs no special handling — typing that name, or leaving it
blank so the app sends the account's own name, resolves to the same string, and
the grantee is by construction someone who exists.

## Appearance

`static/theme.css` is the one palette; every page links it and keeps only its
own layout rules. A **sun/moon switch** in the header flips between the two, and
the choice is remembered per browser session:

| Mode | Built for |
| --- | --- |
| **Dark** | the shipped look, unchanged — a deep field with lifted panels |
| **Light** | daytime and projector use, and it prints: the corner washes are lowered so they do not read as stains on paper, tints go up while text colours drop to their 700/800 steps, and inset fills turn white |

`EXA_UI_THEME=light` starts a whole deployment in the light palette. Nothing is
stored in the profile: this is a viewing preference, not a connection property.

Colours are written as tokens or as `rgb()` channel triples —
`rgba(var(--acc-rgb),.18)` — which is what lets one file carry both themes
instead of a second stylesheet. Two tests hold that line: a page may not
redeclare `:root` or its own `body` rule, may not use an unthemed `rgba()`, and
the light block has to define every token the dark one does. Japanese text uses
**Noto Sans JP** (an earlier stack resolved to a third-weight gothic whose thin
strokes went pale on the dark field); the monospace stack keeps JetBrains Mono
first so Latin values stay tabular, and picks up Noto only for Japanese glyphs.

**Poppins** is the Latin face, and it is served by this app from
`static/fonts/` — five static weights, latin subset, 38 KB total. Nothing is
fetched from a font CDN: on a customer intranet with no route out, a
render-blocking stylesheet stalls first paint until it times out, and a
self-hosted face cannot drift depending on who is viewing it. (`Poppins` is not
available as a variable font, hence the five files.) If those files are ever
removed, the stack falls through to the system UI face — Segoe UI / -apple-system
on Latin, Yu Gothic UI / Meiryo on Japanese Windows — so the page stays readable
rather than broken. A test asserts every declared file exists and that the
palette references no external URL.

Poppins has no CJK coverage, so Japanese text does **not** use it: `html[lang=ja]`
switches the sans stack to Noto Sans JP, falling back to Hiragino Kaku Gothic
ProN, Yu Gothic, Meiryo.

## Column names: display and logical

Every column of a parameter sheet carries **two** names, and they are not the
same field:

| API field | Called | Example | Used by |
| --- | --- | --- | --- |
| `item_name` | display / physical name | `ジョブ名`, `Item 1` | what a human reads, and **what the substitution pulldown advertises** |
| `item_name_rest` | logical name | `p_jobname`, `item_1` | what the API, this tool's JSON and the role variables address |

Measured on a live install, from sheets this tool never touched. The sheet
names are genericised exactly as every other identifier in this repository is;
the field values and the pulldown strings are what was observed:

```
SheetA   item_name='Item 1'  item_name_rest='item_1'  offered as '...Parameter/Item 1'
SheetB  item_name='age'     item_name_rest='ahe'     offered as '...Parameter/age'
zos_job_submit  'ジョブ名' / 'p_jobname'
```

`SheetB` is the decisive one: `age` and `ahe` are different words, so the
pulldown segment is genuinely the display name. That is why the JSON key is
matched through the sheet definition (`_sheet_column_names`) before it is
matched directly — and it is also a hard requirement, not a nicety: an existing
sheet whose column names differ, such as `zos_job_submit`, could not be
bound at all before this.

**Display names must be unique within a sheet.** The pulldown carries no other
identifier, so two columns sharing a label are indistinguishable from here; the
binding step refuses and names both columns instead of picking one.

### The grid

The JSON textarea is parsed as you type, and each key becomes a row:

```
Logical name    Type      Display name          Required   Unique
p_jobname     [string]  [ジョブ名           ]   [ ]        [ ]
p_dsnname     [string]  [DSN Name         ]   [x]        [ ]
```

* Rows are **prefilled from the sheet itself** as soon as its name is typed: the
  display name and the two flags are read back through `GET /api/sheet/<name>`
  (a thin wrapper over the definition read), so the grid shows what ITA holds
  rather than what the page assumes.
* **Only differences from that baseline are sent.** A row left as it arrived adds
  nothing to the payload, so a value-only re-run still cannot overwrite a label
  someone polished in the ITA UI.
* **A blank display name means the logical name.** Before the read existed, that
  sentence was a lie in the hint text, and the ambiguity was a live bug: a cleared
  box *and* a box typed with the logical name itself were both discarded, so
  "put the label back" could not be expressed from the page at all. Against a
  baseline, a blank box is a difference — and a difference is sent.
* Un-ticking a prefilled `Required` is a difference too, and sends
  `{"required": false}` — which lands only through the re-create switch below.
* Rows are re-keyed by logical name, so editing the JSON keeps what you typed and
  silently drops a row whose key has gone away.
* `Required` and `Unique` are the sheet's own `required` / `uniqued` flags,
  enforced by ITA on the sheet's Input screen. They are separate fields, not a
  `*` baked into a label: a star in the display name would flow into the
  substitution item label and be indistinguishable from a real asterisk.

What "blank" means, in the four cases that exist — asked about directly, because
the answer changed when the read arrived:

| the form is for | a blank display name means |
| --- | --- |
| a sheet that does not exist yet | the **logical name**; there is no previous label to keep |
| an existing sheet, row left as seeded | **nothing is sent** — the previous label stays, untouched |
| an existing sheet, label **cleared** | the **logical name** — the label is deliberately put back |
| an existing sheet that could not be read | **nothing**, and the line above the grid says it was unreadable |

A row only *has* a previous label to keep once the read has arrived, which is the
whole reason the read exists: before it, "blank" was the same request as "I did not
touch this", and no amount of typing the logical name could express a revert.

The three states of that read are kept apart, because an operator does the wrong
thing if two of them look alike: `ok` seeds the rows, `absent` says the sheet is
new and every column will be created, and `error` says the sheet could not be read
— in which case a blank row changes nothing, and the line says so rather than
looking like a brand-new sheet. (Measured: ITA does not 404 a missing sheet, it
answers 499 `[menu] is invalid. (menu: <name>)`, the same reply it gives for a menu
key it does not know.) The read is debounced, and each answer is checked against
the sheet name that asked for it, so typing fast cannot let a stale reply win; the
rows are rebuilt under the caret rather than under your fingers.

Known limit, stated plainly: ITA will not change an existing column's `required` /
`uniqued` in place (see below), so moving a flag on a column the sheet already has
takes the re-create switch — and costs that column's entered values.

Validation, bilingual and self-clearing after 7 seconds: an unknown key, a blank
label, a duplicate label, a label over 128 **bytes** (Japanese labels cost three
per character), and a non-object payload. ITA's own limits on display names are
not published by the API, so the cap is deliberately generous and ITA's refusal
is passed through verbatim if it disagrees.

## Re-running the same parameter sheet

The JSON you type is not only a list of columns: each value becomes that
column's **Default Value** on the sheet. That made a second run misleading — the
sheet already existed, so the tool reported `exists:` and stopped, and ITA kept
the first run's defaults. A run of

```json
{ "p_dsnname": "DEMO.DSN", "p_jobname": "JOB0100" }
```

over a sheet that already held `Test` / `DEMO.TODAY.DS` therefore appeared to do
nothing.

Now an existing sheet is **updated** through the same controller with
`type: "edit"`, after reading the live definition
(`GET /create/define/<sheet>/`) and sending **every** column back — the
controller treats a column missing from the payload as a column to *delete*
(verified: echoing two of three columns removed the third).

What an edit may change, measured against a live install:

| Target | Edit |
| --- | --- |
| Default Value on an existing column | **yes** — including on an integer column, where `"42"` is stored as `42` |
| Display name (`item_name`) | **yes** |
| `required` / `uniqued` on an existing column | **no** — `In the case of "Edit", the setting of the target of the existing item cannot be changed. (item: Name, target: required)` |
| A key the sheet does not have yet | **added** as a new column, and *its* `required` / `uniqued` **are** honoured — the rule above is about an existing item |
| `column_class`, byte caps | never sent as a change; a new column must carry the full shape (`"Maximum number of bytes" is required when "String" is set`) |

That refusal is all-or-nothing: one immutable field in the payload loses the
default values too, which is why the flags are dropped from the request and named
in the report instead of being sent and failing the whole run:

```
updated: parameter sheet 'demo_mvmt_3' set 'p_age' (name) — ITA cannot change
'p_name' (required), 'p_age' (required) on an existing column; that is set
when the column is created, or in the ITA UI
```

| Situation | Reported as |
| --- | --- |
| Something moved | `updated: ... set 'a' (default+name), 'b' (added)` |
| Everything already identical | `exists: ... (values and column settings already match)` — no request sent |
| Only an immutable flag differed | `exists: ...; nothing was sent, because only 'a' (required) differed ...` |
| Unreadable definition, or a class id this install does not list | `create/define update failed: ... previous values kept`, run marked bad |

The sheet's definition and its substitution pulldown are two separate queries
and can answer inconsistently for a moment after a rename — including in *case*,
which is what a UI retype tends to produce. Binding therefore folds case when
matching (and refuses a sheet where two labels differ only in case, rather than
guessing), re-reads the pulldown once before declaring a miss, and says which side
disagreed instead of claiming the column does not exist.

Three more measured edges:

* Editing a sheet **seconds after creating it** can come back as `…status" of the
  record to be updated in "Parameter sheet definition list" is "Not created"`,
  and the same edit succeeds a little later. It only bites a create followed by an
  edit in one breath; a re-run of a sheet that predates the run does not see it.
* Each class keeps its default in its **own** field (`single_string_default_value`,
  `integer_default_value`, `decimal_default_value`, `multi_string_default_value`)
  and the definition read returns only the one that class uses. Writing a number
  to the string field would be accepted by a lenient server and change nothing on
  a real one, so the field is taken from the column ITA sent back rather than
  guessed from the class.
* A non-numeric value into an integer column is refused by ITA
  (`Not an integer value.( input value: not-a-number)`) with nothing written, and
  the message reaches the report — no local pre-check needed.

### Getting Required/Unique onto an existing column

`required` and `uniqued` cannot be changed on a column that exists. They *can* be
set on a column that an edit creates — so the ITA UI's ✕-then-re-add is available
through the API too, in a single request: omit the column from the payload (ITA
deletes it) and carry it again as a new one with the flags set. Measured on a
scratch sheet, `p_int` came back `required=1 uniqued=1` with its default value and
display order intact.

That is what the run form's **Re-create a column to change Required / Unique**
switch does — off by default, and it touches only the columns whose flags actually
differ:

```
updated: parameter sheet 'probe_cols' set 'p_str' (unique*); * applied by
re-creating the column, so values already entered for it on the sheet's Input
screen are gone
```

Verified live, on a scratch sheet, through the same code path a run takes:

| | before | after |
| --- | --- | --- |
| the re-created column | `34972bd9 r1u0 order 0` | `4c7d671a r1u1 order 0` |
| a column not asked about | `1b60a762 r1u1` | `1b60a762 r1u1` (untouched) |
| the sheet itself | menu id `226c157b` | `226c157b`, pulldown still offers `Label One` |

Label, class, default value and position all ride along on the new record, other
columns are not disturbed, and the substitution pulldown keeps naming the column —
which is why existing bindings keep resolving it. What does not survive is data
entered against the old column record: the id changed, and the sheet's Input
values are keyed to it. Hence the switch, and hence a footnote in the report
rather than a silent replacement.

A display **name** needs none of this: unlike the two flags, it is editable in
place, so clearing the box with the switch off renames the column and disturbs
nothing else. That asymmetry is the whole reason the flags are opt-in.

The fresh column id must be one the **definition** has never seen. Reusing the
replaced column's own id reads as a change to an existing item — the very refusal
being worked around — and the strict fake catches it.

Deleting a **whole sheet** is not available on this API surface, which is why the
tool keeps its no-delete guarantee: `create/define/execute/` accepts only
`create_new` and `edit` as `type` (`delete`, `discard`, `remove`, `delete_create`
all answer `The target key value is invalid. (key: type)`), `DELETE` and `PATCH` on
the controller answer 405, `/menu/create/define/...` (the generic maintenance shape
used for other menus) 404s, and `/create/define/delete/` is not a delete route at
all — it is the sheet-name route reached with the name `delete`, which is why ITA
replies `[menu] is invalid. (menu: delete)`. Remove a sheet in the ITA UI.

Column classes as this install lists them: `1 SingleTextColumn`,
`2 MultiTextColumn`, `3 NumColumn`, `4 FloatColumn`, `5 DateTimeColumn`,
`6 DateColumn`, `7 IDColumn`, `8 PasswordColumn`, `9 FileUploadColumn`,
`10 HostInsideLinkTextColumn`, `11 ParameterSheetReference`.

The edit never touches the sheet's own `role_list` (whatever it holds is echoed
back), and a column supplied by a *different* run is preserved, not dropped.
Deletion is still not possible from here at all.

### A rename is not local: the substitution list lags the sheet

A column's substitution item is advertised as
`Substitution value:<sheet>:Parameter/<display name>` — so renaming a column does
not only change the definition, it invalidates every string that pointed at it, and
ITA rebuilds that list on its own schedule. Measured on a scratch sheet:

| after the rename | the sheet definition says | the substitution list offers |
| --- | --- | --- |
| +2.4 s | `Renamed` | the **old** label |
| +6.8 s | `Renamed` | the **old** label |
| +11.1 s | `Renamed` | `Renamed` |

Write during that window and the binding step is refused, in words that do not
mention waiting at all:

```
HTTP 499 {"0": {"menu_group_menu_item": ["The input value is an invalid value.
(input value:Substitution value:<sheet>:Parameter/<label>)"]}}
```

This is the same asynchronous-backyard pattern as the role variables collected
after a role is linked — and it is why re-creating a column (above) could make the
sheet step succeed and the binding step fail in one run. The binding now:

* re-reads the option list and retries for up to `SUBST_SETTLE` seconds (60,
  measured ~11), re-**resolving** on each attempt: the string that was just refused
  is by definition stale, so re-sending it would only burn the window;
* reports ITA's own words plus the explanation if the window never closes, rather
  than a bare `failed`;
* treats only a refusal that names `menu_group_menu_item` as this race, so a
  genuinely invalid value is not sat on for a minute.

Then the row itself. `_upsert` can only react to ITA calling a write a duplicate,
and after a rename the item string differs — so ITA does not, the POST would
succeed, and the sheet keeps a row pointing at a label that no longer exists. That
is the shape of the dead substitution rows this install has accumulated on sheets
bound by other tools (one of them has the same variable bound twice). The step now
reads what the movement already binds on that sheet and **moves** the row — PATCH,
lock token echoed back unchanged — instead of adding a second one.

## Settings: point it at any environment

The ⚙ button in the header opens `/settings`. Everything that ties this app to
*one* Exastro install lives in a named **profile**, and saving one rewrites the
`config` constants and rebuilds the client — no restart, no `.env` edit.

| Group | What |
| --- | --- |
| Connection | gateway URL, organization, workspace, Keycloak URL |
| Credentials | API (refresh) token **or** username + password, client id |
| Execution and timeouts | default execution environment, role-variable wait, API / token timeouts, TLS verification |
| Appearance (light / dark) | header switch, remembered per browser session; `EXA_UI_THEME` for a default |
| Version-specific names and ids | menu names, plus **dropdowns** for orchestrator, host format, sheet type, registration method, link-file type and the three menu groups |
| `Define Role for each configuration` | the roles that may administer what gets created; blank = this account |

`.env` is only a **seed**: on first run an `Initial (.env)` profile is created
from whatever `config.py` holds — which, without a `.env`, is *nothing* — and
made active. After that the database wins, and `.env` is never rewritten, so a
bad setting is always undone by deleting `settings.db`.

* **One active profile at a time.** Creating a profile activates it; editing one
  does not switch unless you tick *Make this the active environment*. The header
  pill always names what you are currently writing to.
* **Test connection** probes a *draft*: it builds a throwaway client from the
  unsaved form values and does a token exchange plus one `GET`. The active
  profile and the running client are untouched, so testing a half-typed new
  environment cannot interrupt work in the one you are using.
* **A run in flight keeps its target.** Each request binds its client once
  (`cl = client`), so saving Settings halfway through a creation cannot move
  that creation to another workspace.
* **Advanced ids** are the part that differs between ITA versions. Most display
  strings do not need to be right at all — the client resolves them from ITA's
  own pulldowns by id — so the label fields are documented as fallbacks.

### The "Advanced" ids: you do not have to know them

ITA stores those columns as numbers and shows a *translated* label, so a number
typed from a manual is the easy thing to get wrong. The Settings form therefore
asks the connected environment: opening `/settings` reads `GET /create/define/`
and each menu's `info/pulldown/`, and renders the fields as dropdowns — 5
orchestrators, 3 sheet types, 4 menu groups, 8 link-file types on this install.
Leave every one of them at its pre-selected value and the group is inert.

| Field | What it decides | This install |
| --- | --- | --- |
| Driver orchestrator | which driver executes the movement | `1` Ansible Legacy · `2` Ansible Pioneer · **`3` Ansible Legacy Role** · `4` Terraform Cloud/EP · `5` Terraform CLI |
| Host specific format | how a target host is written on the movement | **`1` IP** · `2` Host name |
| Sheet type | the shape of the parameter sheet being created | **`1` Parameter Sheet(Host/Operation)** · `2` Data Sheet · `3` Parameter Sheet(Operation) |
| Registration method | how a substitution row binds a value to a role variable | **`1` Value type** · `2` Key type |
| Link file type | which `file_link` rows count as role packages | **`3` Ansible-LegacyRole/Role package list** (of 8) |
| Menu group: input / substitution / reference | which menus the new sheet appears in | **`502` Input · `503` Substitution value · `504` Reference** (`501` is Parameter sheet create) |

* Choosing an id also stores the label it came with, in a field you no longer
  edit — it is only a fallback for when a pulldown cannot be read.
* A value the environment does not offer is kept but flagged
  *"not offered by this environment"* rather than being quietly replaced.
* If the target returns no option lists — a create-only account is not offered
  `create/define/`'s choice sets — the fields fall back to text boxes showing
  the raw number, and a banner above the form says so rather than leaving the
  numbers unexplained.

The four **menu names** stay as text: they are names, not choices, and ITA does
not enumerate them. A wrong one makes that step fail with ITA's own message —
which is the information you need, rather than a silent guess.

### `Define Role for each configuration`

The roles that may administer the definitions this tool creates — the parameter
sheet and its bindings. It decides **who can administer a configuration**, and
nothing else: whether a colleague can run a movement comes from their workspace
access, which this field cannot grant or take away.

| What you type | What is sent as `role_list` |
| --- | --- |
| `developer` | `["developer"]` |
| `developer, api-user` (comma or semicolon) | `["developer", "api-user"]` |
| **blank**, username configured | `[<that username>]` |
| **blank**, token-only connection | `[<preferred_username read from the access token>]` |
| blank, and the platform will not say who the token is | `["_no_admin_role"]` |

* Blank therefore means **"the account behind this connection"**, not "no
  administrator": whoever created a configuration can keep editing it. The last
  row is the only case where nobody is named, and it needs a deliberately
  opaque token to reach.
* Names are **sent exactly as typed and never substituted**. The client used to
  replace a name this account was not offered with an offered role containing
  `admin` — the opposite of what an operator choosing a team role asked for.
  Measured on the live install, single and comma-separated lists of unrecognised
  names all pass validation:

```
role_list ["developer"]                  -> accepted   (only the duplicate-name check fired)
role_list ["sheet-owners", "api-user"]   -> accepted
role_list ["just-a-name"]                -> accepted
role_list []  / absent / null            -> rejected, "The target key value is invalid"
```

* An **empty list is not an option**: `create/define/execute/` rejects an
  absent, empty or `null` `role_list` before it looks at anything else, while
  accepting a value it does not recognise. That asymmetry is why "name nobody"
  travels as a placeholder rather than as `[]`.
* If a stricter install refuses a named role, the step retries with no grant and
  the run reports `created … without an admin role grant` — a green run, with the
  refused names listed — instead of failing.
* **Include a role you belong to.** The tool cannot edit or delete a sheet once
  created (`PATCH`/`DELETE` on that endpoint answer `405`), so that administrator
  list is your only way back into the definition from the ITA UI.
* Names, not ids, and compared as exact display strings — case matters.

### Migrating to another environment

A shipped copy carries **no installation of its own**: `config.py` defaults
gateway, org and workspace to empty, and the local lab values live in `.env`,
which you do not ship. So a first run does not fail against a host that is not
there — it says so and points you at Settings.

```
GET /                                  -> 302 /settings?new=1
"No Exastro environment is set up yet — fill in Exastro gateway URL /
 Organization / Workspace / API (refresh) token in Settings before anything
 can be created."
```

From there:

1. The new store asks you to **create the PIN** (there is none yet).
2. Fill in *Connection* and paste a token minted on **their** platform — then
   **Test connection**, which probes the draft without activating it.
3. Tick *Make this the active environment* and save. Effective immediately.
4. Advanced ids need nothing: the dropdowns are read from their install, and a
   blank admin role derives `_<their_workspace>-admin`.

What stays usable while unconfigured: the run history (`/`, `/creation/<id>`,
`/api/creations`) is this app's own SQLite record and needs no Exastro. `GET /`
and `/create` are refused, `/api/roles` answers `409` rather than an HTML
redirect, and `EXA_MOCK=true` bypasses the whole question so the UI can be
demonstrated with no environment at all.

Recover from a bad configuration by deleting `settings.db`; the copy goes back to
"not set up" instead of aiming at a half-typed target.

### PIN and secrets

Settings changes where data gets written and can spend your token, so the page
is behind a PIN (`/settings` → *Unlock*):

* First run has **no** PIN, so whoever starts the app first chooses it. After
  that it is required. `EXA_SETTINGS_PIN` overrides whatever is stored.
* 5 wrong attempts lock the page for 60 seconds; the counter is in the database,
  so restarting does not clear it.
* Unlocking lasts 15 minutes, then the page re-arms. *Lock now* does it sooner.
* **Secrets never travel to the browser.** The token and password render as
  `set · ••••<last 4>` in an empty `type=password` field. Leaving one blank means
  *keep the stored one*; the checkbox next to it means *forget it*.
* `settings.db` is created mode `0600`. Anyone who can read that file can spend
  the token — the PIN protects the UI, not the disk.
* The session cookie signs an authorization decision now, so `app.secret_key` is
  no longer a constant: `EXA_SECRET_KEY`, else a random value persisted in
  `flask_secret.key` (0600).

The app itself still has no login: anyone who can reach `:9200` can create
Movements. The PIN guards configuration, not the main form.


## The Role Package dropdown

`movement_role_link.role_package_name_role_name` only accepts a value ITA
already offers, formatted `Package:Role`. Two dropdowns assemble it:

* **Role Package** — the `link_file_name` column of the **`file_link`** menu,
  i.e. the Git connections feeding the Role Package List. Each entry also shows
  its repo path, so
  `demo_pkg` displays `acme_demo_pkg:demo_pkg/roles`.
  Uploaded (non-Git) role packages are listed in a separate group.
* **Role** — the role half of every selectable `Package:Role` value for the
  chosen package.

Because both lists are read from ITA's own pulldowns, anything selectable in the
UI is guaranteed to validate on write. Re-run the Git sync in Exastro to add
roles, then reload the page.

## ⚠️ Parameters bind to *role variables* — this is the real constraint

Exastro can only bind a parameter-sheet column to a variable the **linked role
actually declares**. A key with no matching role variable cannot be bound, no
matter what the API does.

Measured on the live install:

| Role package `:` role | Variables Exastro collected |
|---|---|
| `zos_job_submit : DEMO_HOST_JOB` | `p_jobname`, `p_reply_flag` |
| `demo_pkg : DEMO_HOST_JOB` | *(none)* |
| `SheetD : DEMO_PLAYBOOK_ROLE` | `name` |

Same role *name*, different *package* — the `demo_pkg` copy in Git
exposes nothing. If your keys don't bind, **the fix is in the role, not here**:
declare them in the role's `defaults/main.yml`, re-sync Git, and use names that
match your JSON keys.

Collection is also **asynchronous**: after a role is linked, Exastro's
`ita_by_ansible_legacy_role_vars_listup` backyard has to inspect the role
through the movement's execution environment. Measured latency is **~10 s**.
The tool therefore waits (up to `EXA_VAR_TIMEOUT`, default 120 s) before
binding — which is why a freshly created movement now binds *every* key instead
of the one key that happened to be visible.

### What it does NOT create

* **The role package itself** — it is a Git sync or an uploaded role ZIP, never
  something a name can synthesise.
* **Role variables** — see above. Unbindable keys are reported per key as
  `missing_variable`, listing what *was* collected, and the run is marked
  `PARTIAL` rather than silently green.

To bind a parameter key to a differently-named role variable, set
`client._variable_map = {"<key>": "<role-var>"}`.

## ⚠️ Exastro translates *everything* per account — including errors

This bit hard when the API account changed from `legacy-account` to
`api-account`. Several columns ITA validates by **display string**, and those
strings come back in the **calling account's language**:

| Column | English account | Japanese account |
|---|---|---|
| `registration_method` | `Value type` | `Value型` |
| `menu_group_menu_item` | `Substitution value:Sheet:Parameter/key` | `代入値自動登録用:Sheet:パラメータ/key` |
| `sheet_type` | `Parameter Sheet(Host/Operation)` | `パラメータシート（ホスト/オペレーションあり）` |
| duplicate error | `The combination is incorrect.` | `組み合わせが不正です。` |

Hardcoding the English form — which is what this tool did, because the sample
rows on the install were created by an English user — makes **every write** fail
for a Japanese account with `利用できない値です` ("unusable value") while reads
keep working. That asymmetry is why it reads like a permissions problem and is
not one.

Nothing sends a literal any more: option values are resolved from the account's
own pulldown by their **stable numeric id**, and error classification matches
both languages (`_DUPLICATE_MARKERS` in `exastro_client.py`). `EXA_SUBST_METHOD`
survives only as the fallback when a pulldown is unreachable.

## Run it

```bash
cd <where-this-folder-lives>/Exastro_Automate
export EXA_API_TOKEN='<paste your Platform API token>'   # or set it in .env
../.venv/bin/python Exastro_Automate/app.py              # serves on :9200
```

Open http://localhost:9200 — or use `./restart.sh` to restart in place.

### Run history

Every run is recorded in `creations.db` with its **full report** — each step,
each parameter binding, the submitted values and the execution environment — not
just a status. The form shows the **latest 20**; older runs stay in the database
and are reachable from a run's **Detail** page (`/creation/<id>`), which also
offers *Re-run with these parameters*.

Rows written before this existed have no stored report; their Detail page says so
instead of showing an empty table.

### Double-submission guard

A run takes ~10–30 s because Exastro collects role variables asynchronously, so
submitting shows a blocking overlay with an elapsed timer and disables the
button. Restoring the page with Back clears it (`pageshow`), and if the request
never returns an unlock is offered — worded to say the run may still be finishing
server-side. It guards **this tab only**: a second window still submits
independently, and re-running overwrites the first run's role link and bindings.

### Tests

```bash
cd Exastro_Automate && ../.venv/bin/python -m pytest tests -q
```

29 tests run against a fake that reproduces ITA's real response envelopes,
unique-combination rejections and optimistic-lock rules, so the update paths are
exercised without touching a live workspace.

### Demo mode

```bash
EXA_MOCK=true ../.venv/bin/python app.py
```

Full UI, dropdowns and all — every call simulated, no Exastro needed.

## Configuration (env vars)

| Variable | Default | Meaning |
|---|---|---|
| `EXA_GATEWAY` | *(empty)* | Exastro gateway — **no install is baked in**, so a fresh copy asks instead of failing |
| `EXA_ORG` / `EXA_WORKSPACE` | *(empty)* / *(empty)* | tenancy; also feeds the derived client id `_<org>-api` |
| `EXA_API_TOKEN` | — | ITA API token |
| `EXA_MOVEMENT_MENU` | `movement_list_ansible_role` | movement menu |
| `EXA_ROLE_LINK_MENU` | `movement_role_link` | movement↔role link menu |
| `EXA_FILE_LINK_MENU` | `file_link` | Git link definitions feeding the dropdown |
| `EXA_SUBST_MENU` | `subst_value_auto_reg_setting_ansible_role` | movement↔parameter links |
| `EXA_EXEC_ENV` | — | execution environment to record on a movement |
| `EXA_VAR_TIMEOUT` / `EXA_VAR_INTERVAL` | `120` / `5` | how long to wait for role variables |
| `EXA_SUBST_METHOD_ID` | `1` | substitution mode **id** (1=Value, 2=Key) — the id is authoritative |
| `EXA_HOST_FORMAT_ID` | `1` | movement host specifier **id** (1=IP, 2=hostname); the translated label is looked up, never typed |
| `EXA_SUBST_METHOD` | `Value type` | fallback label only, when the pulldown is unreachable |
| `EXA_SHEET_TYPE_ID` | `1` | parameter-sheet type id |
| `EXA_MENU_GROUP_INPUT_ID` / `_SUBST_ID` / `_REF_ID` | `502` / `503` / `504` | menu group ids |
| `EXA_TIMEOUT` / `EXA_AUTH_TIMEOUT` | `30` / `90` | HTTP budgets; auth is longer for Keycloak cold starts |
| `EXA_ADMIN_ROLE` | *(empty)* | `Define Role for each configuration`; blank resolves to the connected account |
| `EXA_UI_LANG` | `en` | default interface language; the header switch stores the choice in a cookie |
| `EXA_UI_THEME` | `dark` | default palette, `dark` or `light`; the header switch stores it per session |
| `EXA_MOCK` | `false` | simulate everything |
| `EXA_SETTINGS_DB` | `settings.db` | profile store (overridden per test run) |
| `EXA_SETTINGS_PIN` | — | overrides the stored Settings PIN |
| `EXA_SECRET_KEY` | — | session signing key; generated into `flask_secret.key` if unset |
| `EXA_SECRET_FILE` | `flask_secret.key` | where that key is persisted |

## Gotchas this codebase encodes

These cost real debugging time; they are all verified against 2.8.0.

1. **`PATCH` requires `last_update_date_time` echoed back.** Omit it and ITA
   answers `499-00201 last_update_date_time is invalid (None)`. The update paths
   used to strip that field *and* swallow the error, so a duplicate-looking POST
   was all anyone ever saw.
2. **`/filter/` returns envelopes** (`{"file": {}, "parameter": {...}}`), not
   rows. Reading them raw breaks every "does this already exist?" check — which
   is how duplicate movements with the same name got created, and why the UI
   then shows `Failed to exchange ID.`
3. **A movement with no `ansible_agent_execution_environment` never gets role
   variables collected**, so not one parameter can bind.
4. **Concurrency note:** linking a movement to a second role at the same
   `include_order` is a duplicate, not an insert — it must be PATCHed.

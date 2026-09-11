"""UI text for Exastro_Automate, in two languages.

One flat table of ``key -> {en, ja}``.  ``t()`` is exposed to the templates, so
a page renders entirely in the selected language; nothing is ever shown in both
at once.

Three rules keep this honest:

* **Every key carries both languages.**  A test asserts no entry is missing a
  locale, so a half-translated string cannot slip through.
* **Placeholders are named, not concatenated.**  ``"Create Movement '{name}'"``
  rather than ``"Create Movement " + name``, because word order differs between
  English and Japanese.
* **Stored data stays untranslated.**  Step labels and binding statuses are
  written to the database in English; the display layer looks them up here.  That
  keeps history readable in either language no matter when it was created, and
  leaves ``_looks_bad()`` matching stable English text instead of translated UI.
"""

from __future__ import annotations

SUPPORTED = ("en", "ja")
DEFAULT_LANG = "en"

# Shown in the switcher; always written in its own script.
LANGUAGE_NAMES = {"en": "English", "ja": "日本語"}

TEXT: dict[str, dict[str, str]] = {
    # ---- chrome -----------------------------------------------------------
    "page_title": {"en": "Exastro Movement Creator",
                   "ja": "Exastro 作業実行自動作成"},
    "app_name": {"en": "Exastro Automate", "ja": "Exastro Automate"},
    "app_subtitle": {"en": "Ansible-LegacyRole one-click setup",
                     "ja": "Ansible-LegacyRole 一括設定"},
    "language_switch": {"en": "Language", "ja": "表示言語"},
    "pill_live": {"en": "Live", "ja": "本番接続"},
    "pill_mock": {"en": "Mock Mode", "ja": "モックモード"},

    # ---- form -------------------------------------------------------------
    "new_movement": {"en": "New Movement", "ja": "新規作業実行"},
    "lede": {"en": "The JSON keys become the parameter-sheet columns "
                   "automatically. Fill in the basics and create the whole "
                   "stack in one click.",
             "ja": "JSON のキーはパラメータシートの列として自動作成されます。"
                   "基本情報を入力するだけで、まとめて登録されます。"},
    "movement_name": {"en": "Movement Name", "ja": "作業実行名"},
    "movement_ph": {"en": "e.g. my-app-deploy", "ja": "例: my-app-deploy"},
    "param_rest_name": {"en": "Parameter Rest Name", "ja": "パラメータシート英名"},
    "sheet_hint": {"en": "If no value is filled in, the Movement Name will be "
                         "used as the Rest Parameter Sheet name by default.",
                  "ja": "値が入力されていない場合、既定で作業実行名が Rest "
                        "パラメータシート名として使用されます。"},
    "optional": {"en": "optional", "ja": "任意"},
    "role_package": {"en": "Role Package", "ja": "ロールパッケージ"},
    "role_package_hint": {"en": "Git link file from file_link",
                          "ja": "file_link の Git 連携ファイル"},
    "choose_package": {"en": "— choose a package —", "ja": "— パッケージを選択 —"},
    "choose_role": {"en": "— choose a role —", "ja": "— ロールを選択 —"},
    "no_roles": {"en": "— no roles —", "ja": "— ロールなし —"},
    "group_git": {"en": "Git (file_link)", "ja": "Git 連携 (file_link)"},
    "group_upload": {"en": "Uploaded role packages", "ja": "アップロード済み"},
    "catalogue_unavailable": {"en": "catalogue unavailable",
                              "ja": "一覧を取得できません"},
    "roles_1": {"en": "role", "ja": "ロール"},
    "roles_n": {"en": "roles", "ja": "ロール"},
    "no_roles_warn": {"en": "⚠ no roles synced yet — run the Git sync for "
                             "this link",
                      "ja": "⚠ ロールが未同期です。この連携の Git 同期を"
                            "実行してください"},
    "exec_env": {"en": "Execution Environment", "ja": "実行環境"},
    "exec_env_hint": {"en": "required — Exastro collects role variables "
                            "through it",
                      "ja": "必須 — Exastro はこの環境経由でロール変数を収集します"},
    "exec_env_note": {"en": "A movement without this never gets its role "
                            "variables registered, so no parameter can be "
                            "bound.",
                      "ja": "これを設定しない作業実行はロール変数が登録されない"
                            "ため、パラメータをバインドできません。"},
    "wait_label": {"en": "Wait for Exastro to collect the role's variables "
                         "before binding",
                   "ja": "ロール変数の収集を待ってからバインドする"},
    "wait_max": {"en": "{s}s max — first run after a Git sync needs this",
                 "ja": "最大 {s} 秒 — Git 同期後の初回は必要です"},
    "parameters": {"en": "Parameters (JSON)", "ja": "パラメータ (JSON)"},
    "format": {"en": "Format", "ja": "整形"},
    "keys_note_head": {"en": "Keys must match the role's variable names.",
                       "ja": "キーはロールの変数名と一致する必要があります。"},
    "keys_note_body": {"en": "Each key binds to <movement>:<key> — if the "
                             "linked role does not declare that variable (in "
                             "its defaults/main.yml), the key is reported as "
                             "missing_variable and not bound.",
                       "ja": "各キーは <作業実行>:<キー> にバインドされます。"
                             "リンクされたロールがその変数を defaults/main.yml に"
                             "宣言していない場合、キーは missing_variable として"
                             "報告され、バインドされません。"},
    "detected_columns": {"en": "Detected columns", "ja": "検出された列"},
    # The per-column grid built from the JSON keys. Hints stay one line: the
    # operator is configuring a form, not reading a manual.
    "col_grid_title": {"en": "Column names and flags",
                       "ja": "列の名前と設定"},
        "col_grid_hint": {"en": "Rows start from what the sheet already holds, and "
                             "only differences are sent. A blank display name "
                             "means the logical name.",
                      "ja": "行はシートの現在の設定から始まり、変更された項目のみ"
                            "送信されます。表示名が空なら論理名が使われます。"},
    "replace_flags_label": {"en": "Re-create a column to change Required / Unique",
                           "ja": "必須・一意を変更するため列を作り直す"},
    "replace_flags_hint": {"en": "ITA will not change those on a column that "
                                 "already exists: it is deleted and added back, "
                                 "and values entered for it are lost.",
                           "ja": "既存の列では変更できません。列を削除して再作成し、"
                                 "その列に入力済みの値は失われます。"},
    "grid_seeded": {"en": "Showing what “@@” already holds. A blank display "
                            "name keeps the logical name; clearing a label puts it back.",
                    "ja": "「@@」の現在の設定を表示しています。表示名が空なら論理名が使われ、削除すれば論理名に戻ります。"},
    "grid_new": {"en": "“@@” does not exist yet, so every column will be "
                        "created with these settings.",
                 "ja": "「@@」はまだ存在しないため、すべての列がこの設定で作成されます。"},
    "grid_unreadable": {"en": "“@@” could not be read, so the grid shows "
                              "nothing to keep: blank rows change nothing.",
                        "ja": "「@@」を読み取れなかったため、空の行は変更しません。"},
    "col_logical": {"en": "Logical name", "ja": "論理名"},
    "col_type": {"en": "Type", "ja": "型"},
    "col_display": {"en": "Display name", "ja": "表示名"},
    "col_required": {"en": "Required", "ja": "必須"},
    "col_unique": {"en": "Unique", "ja": "一意"},
    "err_meta_json": {"en": "Column settings are not valid JSON ({exc}).",
                      "ja": "列の設定が JSON として読み込めません（{exc}）。"},
    "err_meta_object": {"en": "Column settings must be an object of "
                              "{logical: {name, required, unique}}.",
                        "ja": "列の設定は {論理名: {name, required, unique}} の形式にしてください。"},
    "err_meta_key": {"en": "Column settings mention '{logic}', which is not a "
                           "parameter in the JSON.",
                     "ja": "列の設定の「{logic}」が JSON のパラメータにありません。"},
    "err_meta_shape": {"en": "Column settings for '{logic}' must list name / "
                             "required / unique.",
                       "ja": "「{logic}」の設定は name / required / unique のみ指定できます。"},
    "err_meta_long": {"en": "Display name for '{logic}' is too long ({got} bytes, "
                            "limit {n}).",
                      "ja": "「{logic}」の表示名が長すぎます（{got} バイト、上限 {n}）。"},
    "err_meta_dup": {"en": "Display name '{name}' is used by both '{a}' and "
                           "'{b}'.",
                     "ja": "表示名「{name}」が「{a}」と「{b}」で重複しています。"},
    "create_btn": {"en": "Create Movement", "ja": "作業実行を作成"},
    "submitting": {"en": "Creating…", "ja": "登録中…"},
    "json_ready": {"en": "ready", "ja": "準備完了"},
    "json_invalid": {"en": "invalid JSON", "ja": "JSON 不正"},
    "json_empty": {"en": "empty", "ja": "空"},
    "json_needs_object": {"en": "needs an object", "ja": "オブジェクトが必要"},
    "cols_1": {"en": "column", "ja": "列"},
    "cols_n": {"en": "columns", "ja": "列"},
    "json_invalid": {"en": "check JSON", "ja": "JSON を確認"},
    "still_working": {"en": "Still working — Exastro is collecting the role's "
                            "variables, which can take up to {s}s.",
                      "ja": "処理中です — Exastro がロール変数を収集中です"
                            "（最大 {s} 秒）"},
    "overlay_title": {"en": "Creating in Exastro…", "ja": "Exastro に登録しています…"},
    "overlay_sub": {"en": "Writing the movement, role link, parameter sheet and "
                          "substitution rows. Please don't close this page or "
                          "submit again.",
                    "ja": "作業実行・ロール関連付け・パラメータシート・代入値を"
                          "登録中です。ページを閉じたり、再送信したりしないで"
                          "ください。"},
    "elapsed": {"en": "{s}s elapsed", "ja": "{s} 秒経過"},
    "unlock": {"en": "Unlock the form anyway", "ja": "そのまま解除する"},
    "unlock_confirm": {"en": "Unlock the form? The run may still be finishing "
                             "in Exastro, and starting another one will "
                             "overwrite the role link and parameter bindings of "
                             "the first.",
                       "ja": "フォームを解除しますか？ 前の処理が Exastro で"
                             "続行中の場合があり、再実行するとロール関連付けと"
                             "パラメータ紐付けが上書きされます。"},

    # ---- history ----------------------------------------------------------
    "history_title": {"en": "Creation history", "ja": "作成履歴"},
    "runs_1": {"en": "{n} run", "ja": "{n} 件"},
    "runs_n": {"en": "{n} runs", "ja": "{n} 件"},
    "runs_kept": {"en": "All runs are kept; this shows {n} at a time",
                  "ja": "全実行を保持しています。{n} 件ずつ表示します"},
    "th_movement": {"en": "Movement", "ja": "作業実行"},
    "th_status": {"en": "Status", "ja": "状態"},
    "th_when": {"en": "When", "ja": "日時"},
    "detail": {"en": "Detail", "ja": "詳細"},
    "tip_full": {"en": "Full report for this run", "ja": "この実行の全レポート"},
    "tip_status_only": {"en": "Only the status was stored for this run",
                        "ja": "この実行は状態のみ保存されています"},
    "nothing_yet": {"en": "Nothing created yet.", "ja": "まだ作成されていません。"},
    "range": {"en": "{start}–{end} of {total}", "ja": "{total} 件中 {start}–{end}"},
    "prev": {"en": "‹", "ja": "‹"},
    "next": {"en": "›", "ja": "›"},

    # ---- result page ------------------------------------------------------
    "result_title": {"en": "Creation result", "ja": "作成結果"},
    "st_created": {"en": "Created", "ja": "作成完了"},
    "st_partial": {"en": "Partial — see steps", "ja": "一部完了"},
    "st_failed": {"en": "Failed", "ja": "失敗"},
    "bound": {"en": "{linked}/{total} parameters bound",
              "ja": "{total} 件中 {linked} 件バインド完了"},
    "kv_movement": {"en": "Movement", "ja": "作業実行"},
    "kv_role": {"en": "Role", "ja": "ロール"},
    "kv_sheet": {"en": "Param sheet", "ja": "パラメータシート"},
    "kv_exec_env": {"en": "Exec env", "ja": "実行環境"},
    "none_val": {"en": "— none —", "ja": "— 未設定 —"},
    "steps": {"en": "Steps", "ja": "手順"},
    "create_another": {"en": "← Create another", "ja": "← もう一度作成"},
    "binding_title": {"en": "Parameter binding", "ja": "パラメータ紐付け"},
    "binding_lede": {"en": "Every JSON key became a parameter-sheet column and "
                           "is bound to its role variable on this movement.",
                     "ja": "JSON のキーはパラメータシートの列として作成され、"
                           "この作業実行のロール変数に紐付けられました。"},
    "th_parameter": {"en": "Parameter", "ja": "パラメータ"},
    "th_type": {"en": "Type", "ja": "型"},
    "th_role_variable": {"en": "Role variable", "ja": "ロール変数"},
    "th_result": {"en": "Result", "ja": "結果"},
    "warn_missing": {"en": "{n} of {total} key(s) had no matching role variable "
                           "on this role. Exastro can only bind a parameter to "
                           "a variable the role actually declares — add them to "
                           "the role's defaults/main.yml, or name the JSON keys "
                           "exactly as the role declares them, then re-run. "
                           "Re-running is safe: existing rows are updated, not "
                           "duplicated.",
                     "ja": "{total} 件中 {n} 件のキーに、対応するロール変数が"
                           "見つかりませんでした。Exastro はロールが宣言している"
                           "変数のみをバインドできます。ロールの defaults/"
                           "main.yml に変数を追加するか、JSON のキーをロールの"
                           "宣言名と一致させて再実行してください。再実行は安全で、"
                           "既存の行は重複せず更新されます。"},

    # steps: ``key`` is the stable identity written into the stored report.
    "step_movement": {"en": "Create Movement '{name}'",
                      "ja": "作業実行を作成 '{name}'"},
    "step_role_link": {"en": "Link Movement <-> Role '{name}'",
                       "ja": "作業実行とロールを関連付け '{name}'"},
    "step_param_sheet": {"en": "Create Parameter Sheet '{name}'",
                         "ja": "パラメータシートを作成 '{name}'"},
    "step_param_link": {"en": "Link Movement <-> Parameter ({count} keys)",
                        "ja": "作業実行とパラメータを関連付け（{count} キー）"},

    # binding statuses, keyed off the stable English status value
    "st_linked": {"en": "linked", "ja": "連携完了"},
    "st_exists": {"en": "exists", "ja": "既存"},
    "st_missing_column": {"en": "missing_column", "ja": "列なし"},
    "st_missing_variable": {"en": "missing_variable", "ja": "ロール変数なし"},
    "st_failed_short": {"en": "failed", "ja": "失敗"},

    # ---- detail page ------------------------------------------------------
    "run": {"en": "Run", "ja": "実行"},
    "run_history_sub": {"en": "Run history", "ja": "実行履歴"},
    "kv_waited": {"en": "Waited for variables", "ja": "変数収集を待った"},
    "yes": {"en": "Yes", "ja": "はい"},
    "no": {"en": "No", "ja": "いいえ"},
    "predates": {"en": "This run predates detailed history, so only its name "
                       "and status were stored. Every run from now on records "
                       "its full steps and parameter bindings.",
                 "ja": "この実行は詳細履歴の導入前のため、名称と状態のみ保存"
                       "されています。以降の実行は手順とパラメータ紐付けのすべてが"
                       "記録されます。"},
    "params_submitted": {"en": "Parameters submitted", "ja": "送信したパラメータ"},
    "rerun": {"en": "Re-run this creation", "ja": "この作成を再実行"},
    "rerun_params": {"en": "Re-run with these parameters", "ja": "この設定で再実行"},
    "back": {"en": "← Back", "ja": "← 戻る"},

    # ---- validation (server-side, shown as banners) -----------------------
    "err_movement_required": {"en": "Movement Name is required.",
                              "ja": "作業実行名は必須です。"},
    "err_role_required": {"en": "Select a Role Package and Role from the lists.",
                          "ja": "ロールパッケージとロールを選択してください。"},
    "err_role_format": {"en": "Role must be 'Package{sep}Role' — got '{got}'.",
                        "ja": "ロールは 'パッケージ{sep}ロール' の形式で"
                              "指定してください（入力: {got}）。"},
    "err_exec_env_required": {"en": "An execution environment is required to "
                                    "collect role variables — choose one, or "
                                    "untick the wait option.",
                              "ja": "実行環境を選択するか、待機オプションを"
                                    "解除してください。"},
    "err_params_required": {"en": "Parameters (JSON) is required.",
                            "ja": "パラメータ (JSON) は必須です。"},
    "err_params_json": {"en": "Parameters is not valid JSON: {exc}",
                        "ja": "JSON の形式が正しくありません: {exc}"},
    "err_params_object": {"en": "Parameters must be a JSON object "
                                "(name: value).",
                          "ja": "パラメータは JSON オブジェクトで指定してください。"},
    "err_params_empty": {"en": "Parameters must contain at least one key.",
                         "ja": "パラメータを 1 つ以上指定してください。"},
    "err_catalogue": {"en": "Could not read the role catalogue from Exastro: {exc}",
                      "ja": "Exastro からロール一覧を取得できませんでした: {exc}"},

    # ---- Settings: chrome and the PIN gate --------------------------------
    "gear": {"en": "Settings", "ja": "設定"},
    "settings_title": {"en": "Settings", "ja": "設定"},
    "settings_lede": {
        "en": "Named connection profiles. Edit or pick one and the app retargets "
              "without a restart.",
        "ja": "名前付き接続プロファイル。選択または編集するだけで、再起動なしに "
              "接続先が切り替わります。"},
    "pin_title": {"en": "Settings are locked", "ja": "設定はロックされています"},
    "pin_explain": {
        "en": "Enter the Settings PIN to view or change the connection.",
        "ja": "接続を確認・変更するには設定 PIN を入力してください。"},
    "pin_label": {"en": "PIN", "ja": "PIN"},
    "pin_button": {"en": "Unlock", "ja": "ロック解除"},
    "pin_choose": {"en": "Choose a Settings PIN", "ja": "設定 PIN を作成"},
    "pin_choose_note": {
        "en": "No PIN exists yet, so whoever sets one first owns it. After this "
              "it is required every time.",
        "ja": "PIN はまだありません。最初に設定した PIN が以降必要になります。"},
    "pin_confirm": {"en": "Repeat PIN", "ja": "PIN を再入力"},
    "pin_created": {"en": "PIN created.", "ja": "PIN を作成しました。"},
    "pin_wrong": {"en": "Incorrect PIN. {left} attempt(s) left.",
                  "ja": "PIN が正しくありません。残り {left} 回。"},
    "pin_short": {"en": "The PIN must be at least 4 characters.",
                  "ja": "PIN は 4 文字以上で入力してください。"},
    "pin_mismatch": {"en": "The two PINs do not match.", "ja": "PIN が一致しません。"},
    "pin_cooldown": {"en": "Too many attempts. Wait {s} seconds.",
                     "ja": "試行回数を超過しました。{s} 秒お待ちください。"},
    "pin_required": {"en": "Unlock Settings with your PIN first.",
                     "ja": "先に PIN で設定のロックを解除してください。"},
    "locked": {"en": "Settings locked.", "ja": "設定をロックしました。"},
    "unlocked_note": {"en": "Unlocked for {minutes} more minute(s).",
                      "ja": "あと {minutes} 分間でロックが戻ります。"},
    "lock": {"en": "Lock now", "ja": "今すぐロック"},
    "back_app": {"en": "Back to the form", "ja": "入力画面へ戻る"},
    "settings_nav": {"en": "Settings", "ja": "設定"},
    "writes_to": {"en": "Writing to", "ja": "接続先"},

    # ---- Settings: groups and fields -------------------------------------
    "group_connection": {"en": "Connection", "ja": "接続先"},
    "group_auth": {"en": "Credentials", "ja": "認証情報"},
    "group_tuning": {"en": "Execution and timeouts", "ja": "実行とタイムアウト"},
    "group_advanced": {"en": "Version-specific names and ids",
                       "ja": "バージョン固有の名称と ID"},
    "advanced_note": {
        "en": "Change these only when the target ITA version names its menus "
              "differently. A wrong value makes every write fail.",
        "ja": "対象の ITA バージョンでメニュー名が異なる場合のみ変更してください。"
              "誤ると登録操作はすべて失敗します。"},
    "set_gateway": {"en": "Exastro gateway URL", "ja": "Exastro ゲートウェイ URL"},
    "set_gateway_hint": {
        "en": "Scheme, host and port. The API path is built from organization "
              "and workspace.",
        "ja": "スキーマ・ホスト・ポートまで。API パスは組織とワークスペースから "
              "組み立てます。"},
    "set_org": {"en": "Organization", "ja": "組織"},
    "set_workspace": {"en": "Workspace", "ja": "ワークスペース"},
    "set_keycloak": {"en": "Keycloak URL (optional)", "ja": "Keycloak URL（任意）"},
    "set_keycloak_hint": {
        "en": "Unused by default: auth is proxied by the gateway at "
              "/auth/realms/<org>.",
        "ja": "既定では未使用。認証はゲートウェイの /auth/realms/<org> 経由です。"},
    "set_token": {"en": "API (refresh) token", "ja": "API トークン（refresh token）"},
    "set_token_hint": {
        "en": "Minted in the Platform UI, and preferred over a password. Leave "
              "blank to keep the stored token.",
        "ja": "Platform UI で発行します（パスワードより優先）。空白なら保存済み "
              "トークンを維持します。"},
    "set_user": {"en": "Username", "ja": "ユーザー名"},
    "set_user_hint": {"en": "Used only when no API token is set.",
                      "ja": "API トークンが無い場合にのみ使用します。"},
    "set_password": {"en": "Password", "ja": "パスワード"},
    "set_client_id": {"en": "Keycloak client id (optional)",
                      "ja": "Keycloak クライアント ID（任意）"},
    "set_client_id_hint": {"en": "Blank means _<org>-api.",
                           "ja": "空白なら _<org>-api。"},
    "set_exec_env": {"en": "Default execution environment",
                     "ja": "既定の実行環境"},
    "set_exec_env_hint": {"en": "Used when the form leaves it empty.",
                          "ja": "入力画面で未選択の場合に使用します。"},
    "set_var_timeout": {"en": "Role variable wait (s)", "ja": "ロール変数待機（秒）"},
    "set_timeout": {"en": "API timeout (s)", "ja": "API タイムアウト（秒）"},
    "set_auth_timeout": {"en": "Token timeout (s)", "ja": "トークン取得待機（秒）"},
    "set_verify_tls": {"en": "Verify TLS certificate", "ja": "TLS 証明書を検証する"},
    "set_verify_tls_hint": {
        "en": "Off is normal for a self-signed lab gateway.",
        "ja": "自己署名の実験環境では通常オフです。"},
    "set_admin_role": {"en": "Define Role for each configuration",
                       "ja": "各設定の定義ロール"},
    "set_admin_role_hint": {
        "en": "If blank, the existing role will be used by default.",
        "ja": "空白の場合は、既存のロールが既定で使用されます。"},
    # --- first-run state: a copy that has never been pointed at an install ----
    "theme_switch": {"en": "Appearance", "ja": "表示テーマ"},
    "theme_light": {"en": "Light", "ja": "ライト"},
    "theme_dark": {"en": "Dark", "ja": "ダーク"},
    "theme_set": {"en": "Appearance set to {mode}.",
                  "ja": "表示を{mode}に切り替えました。"},
    "setup_needed": {
        "en": "No Exastro environment is set up yet — fill in {fields} in "
              "Settings before anything can be created.",
        "ja": "Exastro 接続先が未設定です。作成の前に Settings で {fields} を "
              "入力してください。"},
    "setup_banner": {
        "en": "Not connected yet. This copy ships without an install baked in, "
              "so the first task is to describe the target below.",
        "ja": "まだ接続していません。このアプリは接続先を内蔵していないため、"
              "まず以下の設定が必要です。"},
    "setup_missing": {"en": "Still to set", "ja": "未設定"},
    # --- Advanced: menu names ------------------------------------------------
    "adv_movement_menu": {"en": "Movement menu", "ja": "作業実行メニュー"},
    "adv_role_menu": {"en": "Role menu", "ja": "ロールメニュー"},
    "adv_role_link_menu": {"en": "Role link menu", "ja": "ロール関連付けメニュー"},
    "adv_file_link_menu": {"en": "File link menu", "ja": "ファイル関連付けメニュー"},
    "adv_subst_menu": {"en": "Substitution menu", "ja": "代入値自動登録メニュー"},
    "adv_execute_menu": {"en": "Execute menu", "ja": "作業実行メニュー名"},
    "adv_header_section": {"en": "Header section", "ja": "ヘッダー項目"},
    # --- Advanced: ids, shown as dropdowns read from the environment ---------
    "adv_orchestrator_id": {"en": "Driver orchestrator", "ja": "オーケストレーター"},
    "adv_host_format_id": {"en": "Host specific format", "ja": "ホスト指定形式"},
    "adv_sheet_type_id": {"en": "Sheet type", "ja": "シート種別"},
    "adv_subst_method_id": {"en": "Registration method", "ja": "登録方法"},
    "adv_link_file_type_id": {"en": "Link file type", "ja": "リンクファイル種別"},
    "adv_group_input_id": {"en": "Menu group: input", "ja": "メニューグループ：入力用"},
    "adv_group_subst_id": {"en": "Menu group: substitution",
                           "ja": "メニューグループ：代入値自動登録用"},
    "adv_group_ref_id": {"en": "Menu group: reference", "ja": "メニューグループ：参照用"},
    "id_wins_hint": {
        "en": "ITA checks the label, not the number, so both are sent.",
        "ja": "ITA は番号ではなく表示名で照合するため、両方を送信します。"},
    # --- the stored labels behind those ids (auto-managed, not editable) ----
    "adv_orchestrator": {"en": "Driver orchestrator (fallback label)",
                          "ja": "オーケストレーター（代替ラベル）"},
    "adv_host_format": {"en": "Host specific format (fallback label)",
                        "ja": "ホスト指定形式（代替ラベル）"},
    "adv_subst_method": {"en": "Registration method (fallback label)",
                         "ja": "登録方法（代替ラベル）"},
    "adv_link_file_type": {"en": "Link file type (fallback label)",
                           "ja": "リンクファイル種別（代替ラベル）"},
    "detected_hint": {
        "en": "Offered by the connected environment. Pick the meaning you want; "
              "the number is ITA's, not yours to remember.",
        "ja": "接続先の環境が返す選択肢です。数値ではなく意味で選んでください。"},
    "detected_unreachable": {
        "en": "This account was not offered the option lists for some fields "
              "below, so they show raw numbers. That is ITA's permission set, "
              "not a bug here — the numbers are used exactly as they stand.",
        "ja": "一部の項目について接続先から選択肢が返されなかったため、数値を直接 "
              "表示しています。これは ITA の権限設定によるもので、値はそのまま "
              "使われます。"},
    "not_offered": {"en": "not offered by this environment",
                    "ja": "この環境の選択肢に無い値"},

    # ---- Settings: profiles and actions ----------------------------------
    "profiles_title": {"en": "Connection profiles", "ja": "接続プロファイル"},
    "profiles_cred": {"en": "Credential", "ja": "認証情報"},
    "active_badge": {"en": "active", "ja": "使用中"},
    "set_profile_name": {"en": "Profile name", "ja": "プロファイル名"},
    "set_new": {"en": "New profile", "ja": "新規プロファイル"},
    "set_edit": {"en": "Edit", "ja": "編集"},
    "set_delete": {"en": "Delete", "ja": "削除"},
    "set_activate": {"en": "Use this", "ja": "これを使用"},
    "set_save": {"en": "Save profile", "ja": "プロファイルを保存"},
    "set_test": {"en": "Test connection", "ja": "接続テスト"},
    "make_active": {"en": "Make this the active environment",
                    "ja": "この環境を使用中にする"},
    "set_saved": {"en": "Profile '{name}' saved.",
                  "ja": "プロファイル '{name}' を保存しました。"},
    "set_active_now": {"en": "Now targeting '{name}'.",
                       "ja": "'{name}' に切り替えました。"},
    "set_deleted": {"en": "Profile deleted.", "ja": "プロファイルを削除しました。"},
    "set_no_profile": {"en": "No such profile.", "ja": "プロファイルが見つかりません。"},
    "set_name_required": {"en": "Give the profile a name.",
                          "ja": "プロファイル名を入力してください。"},
    "set_required": {"en": "{field} is required.", "ja": "{field} は必須です。"},
    "set_number": {"en": "{field} must be a whole number.",
                   "ja": "{field} は整数で入力してください。"},
    "set_range": {"en": "{field} must be between {lo} and {hi}.",
                  "ja": "{field} は {lo}〜{hi} の範囲で入力してください。"},
    "set_url_scheme": {"en": "The gateway URL must start with http:// or https://.",
                       "ja": "ゲートウェイ URL は http:// または https:// で始めてください。"},
    "set_save_failed": {"en": "Could not save the profile: {error}",
                        "ja": "プロファイルを保存できませんでした: {error}"},
    "set_test_ok": {
        "en": "Connection OK — token in {token_ms} ms, API in {api_ms} ms. "
              "{base} offers {sheet_types} sheet type(s) and {menu_groups} "
              "menu group(s).",
        "ja": "接続 OK — トークン {token_ms} ms、API {api_ms} ms。{base} で "
              "シートタイプ {sheet_types} 件、メニューグループ {menu_groups} 件。"},
    "set_test_fail": {"en": "Connection failed: {error}",
                      "ja": "接続に失敗しました: {error}"},
    "secret_set": {"en": "Stored. Leave blank to keep it.",
                   "ja": "保存済み。空白のままなら維持します。"},
    "secret_unset": {"en": "not set", "ja": "未設定"},
    "target_preview": {"en": "Effective target", "ja": "有効な接続先"},
    "no_profiles": {"en": "No profiles yet.", "ja": "プロファイルがありません。"},
    "delete_confirm": {
        "en": "Delete this profile? Exastro objects it created are not removed.",
        "ja": "このプロファイルを削除しますか？作成済みの Exastro オブジェクトは "
              "削除されません。"},
}


def normalize(lang: str | None) -> str:
    """Map anything the outside world sends to a supported code."""
    code = (lang or "").strip().lower()
    if code in SUPPORTED:
        return code
    # 'ja-JP', 'en_GB', a bare 'j'... anything recognisable wins its language.
    for supported in SUPPORTED:
        if code.startswith(supported):
            return supported
    return DEFAULT_LANG


def t(key: str, lang: str = DEFAULT_LANG, **fmt) -> str:
    """Translate ``key`` into ``lang``, formatting any named placeholders.

    A missing key returns the key itself in brackets rather than raising: a
    typo is visible on screen instead of taking the page down.
    """
    entry = TEXT.get(key)
    if entry is None:
        return f"[{key}]"
    text = entry.get(normalize(lang)) or entry.get(DEFAULT_LANG) or f"[{key}]"
    if fmt:
        try:
            return text.format(**fmt)
        except (KeyError, IndexError):
            # Wrong placeholders are a programming error, but a page that shows
            # the raw template beats one that 500s on a formatting slip.
            return text
    return text

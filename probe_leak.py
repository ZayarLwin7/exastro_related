import os, tempfile, pathlib
tmp = tempfile.mkdtemp()
os.environ["EXA_SETTINGS_DB"] = str(pathlib.Path(tmp)/"s.db")
os.environ["EXA_DB"] = str(pathlib.Path(tmp)/"c.db")
import config as cfg; cfg.MOCK = True
import settings
settings.SETTINGS_DB = os.environ["EXA_SETTINGS_DB"]; settings.init_store()
import app as appmod
appmod.settings = settings; appmod.DB_PATH = os.environ["EXA_DB"]; appmod.init_db()
appmod.app.config["TESTING"] = True

settings.create_user("admin1","admin-password")
settings.adopt_orphans("admin1")
ap = settings.save_profile("ADMIN-PROD", {
    "GATEWAY_URL":"http://admin-only:4040","ORG_ID":"secrets",
    "WORKSPACE_ID":"prod_ws","API_TOKEN":"ADMIN-SECRET-TOKEN"}, owner="admin1")
settings.activate_profile(ap, owner="admin1")
settings.create_user("newbie","newbie-password")
c = appmod.app.test_client()
with c.session_transaction() as s: s["user"]="newbie"
cl = appmod.client_for("newbie")
print("newbie's own profile:", settings.own_active_profile("newbie"))
print("newbie client GATEWAY :", repr(cl._v("GATEWAY_URL")))
print("newbie client ORG_ID  :", repr(cl._v("ORG_ID")))
print("newbie client API_TOKEN:", repr(cl._v("API_TOKEN"))[:30])
rc = appmod._request_config.__wrapped__ if hasattr(appmod._request_config,'__wrapped__') else None
from flask import request
with appmod.app.test_request_context("/"):
    appmod.session["user"]="newbie"
    print("page shows gateway   :", repr(appmod._request_config().get("gateway")))
    print("page shows org       :", repr(appmod._request_config().get("org")))
    body = c.get("/").get_data(as_text=True)
    print("admin token on page  :", "ADMIN-SECRET-TOKEN" in body)
    print("admin profile name shown:", "ADMIN-PROD" in body)

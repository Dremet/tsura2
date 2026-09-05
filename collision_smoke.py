"""Round-trip the new "Vehicle collision" block through every panel that has it.

Renders each panel, posts back exactly what it rendered plus a collision block,
and checks what lands in the config. CONFIG_DIR points at a temp copy of the
live configs, so nothing the game servers read is touched.
"""
import html as htmlmod
import json, os, re, shutil, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tsura.app import create_app
from tsura.app.blueprints.admin import routes

# The consumer side lives in the tsura_server_scripts repo. When a checkout is
# next to this one the test also asserts the commands the game server gets;
# without it the config round-trip is still fully checked.
SCRIPTS_REPO = os.environ.get(
    "TSURA_SERVER_SCRIPTS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "tsura_server_scripts"))
try:
    sys.path.insert(0, os.path.join(
        SCRIPTS_REPO, "tripleheat/server/config/Scripts"))
    import webconfig
except ImportError:
    webconfig = None
    print("no tsura_server_scripts checkout — skipping the command assertions")

OWNER = routes.OWNER_STEAM_ID
PANELS = [("tripleheat", "/admin/tripleheat", routes.tripleheat),
          ("casual_heat", "/admin/casual-heat", routes.casual_heat),
          ("hotlapping", "/admin/hotlapping", routes.hotlapping),
          ("topdown", "/admin/topdown", routes.topdown)]

tmp = tempfile.mkdtemp(prefix="cfg-")
for f in os.listdir("/srv/tsura/server_config"):
    if f.endswith(".json") and not f.startswith("."):
        shutil.copyfile(f"/srv/tsura/server_config/{f}", os.path.join(tmp, f))
routes.CONFIG_DIR = tmp
routes._csrf_ok = lambda: True
app = create_app()
from flask import g


def fields(html):
    """Named inputs as a browser would post them (attributes decoded)."""
    out = {}
    for m in re.finditer(r"<input[^>]*>", html):
        tag = m.group(0)
        name = re.search(r'name="([^"]+)"', tag)
        if not name:
            continue
        value = re.search(r'value="([^"]*)"', tag)
        if 'type="checkbox"' in tag:
            out[name.group(1)] = "1" if "checked" in tag else None
        else:
            out[name.group(1)] = value.group(1) if value else ""
    for m in re.finditer(r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', html, re.S):
        sel = re.search(r'<option value="([^"]*)"[^>]*selected', m.group(2))
        out[m.group(1)] = sel.group(1) if sel else ""
    for m in re.finditer(r'<textarea[^>]*name="([^"]+)"[^>]*>(.*?)</textarea>', html, re.S):
        out[m.group(1)] = m.group(2)
    return {k: (htmlmod.unescape(v) if isinstance(v, str) else v)
            for k, v in out.items()}


def call(path, view, method="GET", data=None):
    with app.test_request_context(path, method=method, data=data):
        g.current_steam_id, g.session_id = OWNER, "test"
        return view()


def cfg_of(server):
    with open(os.path.join(tmp, f"{server}.json"), encoding="utf-8") as fh:
        return json.load(fh)


failures = []
for server, path, view in PANELS:
    html = call(path, view)
    assert "Vehicle collision" in html, f"{server}: collision card missing"
    form = {k: v for k, v in fields(html).items() if v is not None}
    form["csrf_token"] = ""

    # 1) switch it on
    form.update({"collision_enabled": "1", "collision_collisionSteadiness": "70",
                 "collision_collisionExtraStability": "12.5",
                 "collision_stuckAvoidance": "0"})
    call(path, view, "POST", form)
    got = cfg_of(server).get("collision")
    want = {"enabled": True, "collisionSteadiness": 70,
            "collisionExtraStability": 12.5, "stuckAvoidance": 0}
    if got != want:
        failures.append(f"{server}: saved {got!r}, wanted {want!r}")
        continue
    cmds = webconfig.get_collision_commands(cfg_of(server)) if webconfig else None
    assert cmds is None or cmds == ["/physics.adjustVehicleCollisionSettings = 1",
                    "/physics.collisionSteadiness = 70",
                    "/physics.collisionExtraStability = 12.5",
                    "/physics.stuckAvoidance = 0"], cmds

    # 2) the panel shows it back
    html = call(path, view)
    again = fields(html)
    assert again.get("collision_enabled") == "1", f"{server}: box not re-checked"
    assert again.get("collision_collisionSteadiness") == "70", server

    # 3) switch it off again -> master switch goes back off
    form = {k: v for k, v in again.items() if v is not None}
    form["csrf_token"] = ""
    form.pop("collision_enabled", None)
    call(path, view, "POST", form)
    off = cfg_of(server).get("collision")
    # the numbers stay, so re-ticking the box restores them
    assert off == {**want, "enabled": False}, f"{server}: off stored {off!r}"
    assert not webconfig or webconfig.get_collision_commands(cfg_of(server)) == [
        "/physics.adjustVehicleCollisionSettings = 0"], server

    # 4) out-of-range value is rejected and leaves the config alone
    form["collision_enabled"] = "1"
    form["collision_collisionSteadiness"] = "500"
    call(path, view, "POST", form)
    assert cfg_of(server).get("collision") == {**want, "enabled": False}, \
        f"{server}: bad value was saved anyway"
    print(f"{server:12} on -> shown -> off -> bad value rejected   OK")

shutil.rmtree(tmp)
if failures:
    print("\n".join(failures)); sys.exit(1)
print("\nOK")

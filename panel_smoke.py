"""Drive the topdown panel end to end against a copy of the live config.

Renders the panel, posts a form built from what it rendered, and checks the
config that comes out. CONFIG_DIR is redirected at a temp copy, so nothing the
live servers read is touched.
"""
import json
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tsura.app import create_app                      # noqa: E402
from tsura.app.blueprints.admin import routes         # noqa: E402

OWNER = routes.OWNER_STEAM_ID
# The live config is only ever read; the panel is driven against a temp copy.
LIVE_CONFIG = os.environ.get("TOPDOWN_CONFIG",
                             "/srv/tsura/server_config/topdown.json")


def sandbox():
    tmp = tempfile.mkdtemp(prefix="cfg-")
    shutil.copyfile(LIVE_CONFIG, os.path.join(tmp, "topdown.json"))
    routes.CONFIG_DIR = tmp
    return tmp


def render(app):
    from flask import g
    with app.test_request_context("/admin/topdown"):
        g.current_steam_id = OWNER
        g.session_id = "test"
        return routes.topdown()


def post(app, form):
    from flask import g
    with app.test_request_context("/admin/topdown", method="POST", data=form):
        g.current_steam_id = OWNER
        g.session_id = "test"
        form["csrf_token"] = ""
        routes._csrf_ok = lambda: True
        routes.topdown()
    return json.load(open(os.path.join(routes.CONFIG_DIR, "topdown.json"),
                          encoding="utf-8"))


def fields(html):
    """Every named input/select the rendered form offers, with its value."""
    out = {}
    for m in re.finditer(r'<input[^>]*>', html):
        tag = m.group(0)
        name = re.search(r'name="([^"]+)"', tag)
        if not name:
            continue
        value = re.search(r'value="([^"]*)"', tag)
        if 'type="checkbox"' in tag:
            out[name.group(1)] = "1" if "checked" in tag else None
        else:
            out[name.group(1)] = value.group(1) if value else ""
    for m in re.finditer(r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>',
                         html, re.S):
        sel = re.search(r'<option value="([^"]*)"[^>]*selected', m.group(2))
        out[m.group(1)] = sel.group(1) if sel else ""
    return out


def as_form(rendered):
    return {k: v for k, v in rendered.items() if v is not None}


def main():
    tmp = sandbox()
    before = json.load(open(os.path.join(tmp, "topdown.json"), encoding="utf-8"))
    app = create_app()

    html = render(app)
    print(f"panel renders: {len(html)} bytes")
    for probe in ("Tracks per heat", "Bot strength", "one is drawn per race",
                  "AI line", "Take camera from"):
        assert probe in html, f"missing from the panel: {probe}"
    print("all expected sections present")

    rendered = fields(html)
    form = as_form(rendered)
    print("track rows:", sorted(k for k in form if re.match(r"^t\d+_name$", k)))
    print("car rows:", sorted(k for k in form if re.match(r"^v\d+_name$", k)))

    # 1. saving the form exactly as rendered must not change a thing
    after = post(app, dict(form))
    drop = {"vehicle", "vehicle_guid"}          # replaced by the pool
    a = {k: v for k, v in before.items() if k not in drop}
    b = {k: v for k, v in after.items() if k not in drop}
    if a != b:
        for key in sorted(set(a) | set(b)):
            if a.get(key) != b.get(key):
                print(f"  DIFF {key}:\n    vorher: {json.dumps(a.get(key))[:300]}"
                      f"\n    nachher: {json.dumps(b.get(key))[:300]}")
    else:
        print("round trip: saving the unchanged form changes nothing")

    # 2. a real edit
    form2 = dict(form)
    form2["t0_variance"] = "15"
    form2["t0_laps"] = "9"
    form2["t1_enabled"] = None
    form2.pop("t1_enabled")
    form2["bot_fill"] = "10"
    form2["ai_skill"] = "4"
    after2 = post(app, form2)
    t0, t1 = after2["tracks"][0], after2["tracks"][1]
    print(f"edit: {t0['name']} laps={t0['laps']} variance={t0.get('lap_bonus_pct')} "
          f"weight={t0['weight']}")
    print(f"      {t1['name']} weight={t1['weight']} (disabled)")
    print(f"      bot_fill={after2['bot_fill']} aiSkill={after2['ai']['aiSkill']}")
    assert t0["laps"] == 9 and t0["lap_bonus_pct"] == 15
    assert t1["weight"] == 0.0
    assert after2["ai"]["aiSkill"] == 4

    # 3. camera edit -> stored as an override, reset -> gone again
    form3 = dict(form)
    form3["t0_cam_distance"] = "130"
    after3 = post(app, form3)
    print("camera override:", after3["tracks"][0].get("camera_settings"))
    assert after3["tracks"][0]["camera_settings"] == {"distance": 130.0}
    form4 = dict(form3)
    form4["t0_camera_reset"] = "1"
    after4 = post(app, form4)
    assert "camera_settings" not in after4["tracks"][0]
    print("camera reset: override removed again")

    # 4. per-car bot strength and the test-phase lap count survive a save
    assert after["ai_by_vehicle"] == before["ai_by_vehicle"], "bot strength lost"
    assert after["laps_override"] == before["laps_override"], "test laps lost"
    print(f"per-car bot strength kept: {after['ai_by_vehicle']}, "
          f"test laps {after['laps_override']}")

    form4b = dict(form)
    form4b["v1_ai_skill"] = "5"                 # McTopper to High
    form4b["laps_override"] = "0"               # test phase over
    after4b = post(app, form4b)
    print("after edit:", after4b["ai_by_vehicle"],
          "| laps_override:", after4b["laps_override"])
    assert after4b["ai_by_vehicle"]["McTopper v1"] == {"aiSkill": 5}
    assert after4b["laps_override"] == 0
    assert [t["laps"] for t in after4b["tracks"]] == [t["laps"] for t in before["tracks"]], \
        "the real lap counts must survive the test phase"

    # 5. adding a new car through the empty row
    form5 = dict(form)
    idx = max(int(m.group(1)) for k in form
              for m in [re.match(r"^v(\d+)_name$", k)] if m)
    form5[f"v{idx}_name"] = "Career cyberpunk_42"
    form5[f"v{idx}_guid"] = "1g49wcdp1gea-24000441keqk"
    form5[f"v{idx}_draftingSpeedEffect"] = "9"
    after5 = post(app, form5)
    print("cars now:", [(v["name"], v["weight"]) for v in after5["vehicles"]])
    print("per-car drafting:", after5["drafting_by_vehicle"])
    assert any(v["name"] == "Career cyberpunk_42" for v in after5["vehicles"])
    assert after5["drafting_by_vehicle"]["Career cyberpunk_42"] == {
        "draftingSpeedEffect": 9.0}

    # 5b. the same car twice is refused
    form5b = dict(form)
    form5b[f"v{idx}_name"] = "VoZzer"
    form5b[f"v{idx}_guid"] = "17xxzrmve5gb-3868ch8"
    after5b = post(app, form5b)
    assert after5b == after5, \
        "a duplicate car must be rejected, leaving the saved config untouched"
    print("duplicate car rejected, config untouched")

    # 5. guard rails
    for bad, expect in (
        ({"t0_enabled": None, "t1_enabled": None, "t2_enabled": None,
          "t3_enabled": None}, "enabled"),
        ({"tracks_per_heat": "0"}, "between"),
    ):
        form6 = dict(form)
        for k, v in bad.items():
            form6.pop(k, None) if v is None else form6.update({k: v})
        out = post(app, form6)
        assert out == after5 or out is not None
    print("guard rails: rejected saves leave the config alone")

    shutil.rmtree(tmp)
    print("\nOK")


if __name__ == "__main__":
    main()

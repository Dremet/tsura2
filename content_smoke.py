"""Check that installed tracks and cars are readable, offered and saveable.

The panel used to build its name lists purely from the race database, so
freshly uploaded content could neither be picked nor saved — and on TopDown,
which needs GUIDs, not at all. This drives the file reader against every .lvl
and .veh on the servers this user may read, using the race database as ground
truth: every file whose GUID the database knows must yield exactly the name
the database recorded.

Run it as a user in group `tsu` (the site runs as `tsura`); servers whose
folders are not readable are skipped rather than failed.
"""
import getpass
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tsura.app import create_app                              # noqa: E402
from tsura.app.blueprints.admin import routes                 # noqa: E402
from tsura.app.blueprints.admin.content_files import (        # noqa: E402
    ContentFileError, read_content_file)
from flask import g                                           # noqa: E402

SERVERS = ("topdown", "tripleheat", "casual_heat", "hotlapping", "events")


def ground_truth():
    """GUID -> name for everything ever raced on a TSURA server."""
    truth = {}
    for column, name_column in (("track_guid", "track_name"),
                                ("vehicle_guid", "vehicle_name")):
        # _guid_lookup is name -> GUID; we key on the GUID, which is stable.
        truth.update({guid: name
                      for name, guid in routes._guid_lookup(
                          column, name_column).items()})
    return truth


def readable(server):
    try:
        os.listdir(routes._upload_dir(server, "track"))
        return True
    except OSError:
        return False


def main():
    app = create_app()
    with app.test_request_context("/admin/"):
        g.current_steam_id, g.session_id = routes.OWNER_STEAM_ID, "test"
        truth = ground_truth()
        print(f"ground truth from the race DB: {len(truth)} name/GUID pairs")

        checked = mismatched = unreadable = 0
        skipped = []
        for server in SERVERS:
            if not readable(server):
                skipped.append(server)
                continue
            home = os.path.dirname(routes._upload_dir(server, "track"))
            for pattern in ("Levels/*.lvl", "Vehicles/*.veh"):
                for path in glob.glob(os.path.join(home, pattern)):
                    try:
                        got = read_content_file(path)
                    except ContentFileError:
                        unreadable += 1
                        continue
                    want = truth.get(got["guid"])
                    if want is None:
                        continue                  # never raced: nothing to check
                    checked += 1
                    if want != got["name"]:
                        mismatched += 1
                        print(f"  MISMATCH {os.path.basename(path)}: "
                              f"file={got['name']!r} db={want!r}")
        if skipped:
            print(f"skipped (folders not readable as {getpass.getuser()!r}): "
                  f"{', '.join(skipped)}")
        print(f"checked against the DB: {checked}   mismatched: {mismatched}   "
              f"unreadable: {unreadable}")
        assert checked > 50, "too few files checked to mean anything"
        assert mismatched == 0, "the file reader disagrees with the race DB"

        # A track installed on a server but never raced must still be offered
        # and must survive the save check without "Allow new names".
        server = "topdown" if readable("topdown") else None
        if server:
            installed = routes._server_content(server)["track"]
            raced = set(routes._known_names(
                "SELECT DISTINCT track_name AS name FROM mart.v_race_results"))
            fresh = sorted(n for n in installed if n not in raced)
            assert fresh, "no never-raced track installed — nothing to prove"
            name = fresh[0]
            print(f"never-raced track on {server}: {name!r} "
                  f"-> guid {installed[name]}")
            assert name in routes._known_tracks(server), "not offered in the list"
            routes._check_content_names("track", [name], server)
            print("save check accepts it without 'Allow new names'")

            tracks, cars = routes._topdown_known_guids()
            assert tracks.get(name), "TopDown would still refuse: no GUID"
            print(f"TopDown has a GUID for it: {tracks[name]}")

            try:
                routes._check_content_names(
                    "track", ["Definitely Not A Real Track"], server)
            except ValueError:
                print("a made-up name is still rejected")
            else:
                raise AssertionError("a made-up name was accepted")
    print("\nOK")


if __name__ == "__main__":
    main()

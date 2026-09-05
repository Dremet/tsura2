"""Check the .aid upload path and the admin landing page.

Uploads go into a temp directory, not into the running server's AI folder.
"""
import io
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import g                                   # noqa: E402
from tsura.app import create_app                      # noqa: E402
from tsura.app.blueprints.admin import routes         # noqa: E402

OWNER = routes.OWNER_STEAM_ID
GOOD = "ai-139k2kmmzws3-33vswqr-17xxzrmve5gb-3868ch8.aid"


def upload(app, filename, tmp):
    routes._upload_dir = lambda server, kind: tmp
    routes._csrf_ok = lambda: True
    data = {"files": (io.BytesIO(b"AIDATA"), filename)}
    with app.test_request_context("/admin/topdown/upload/ai_line",
                                  method="POST", data=data):
        g.current_steam_id = OWNER
        g.session_id = "test"
        messages = []
        routes.flash = lambda msg, cat="message": messages.append((cat, msg))
        routes.upload("topdown", "ai_line")
        return messages


def main():
    app = create_app()
    tmp = tempfile.mkdtemp(prefix="ai-")

    msgs = upload(app, GOOD, tmp)
    print("valid name  ->", msgs[0][0], "|", msgs[0][1][:110])
    assert msgs[0][0] == "success"
    assert os.path.exists(os.path.join(tmp, GOOD))
    assert "next heat" in msgs[0][1]

    for bad in ("driving-lines.aid", "ai-something.aid", "line.txt"):
        msgs = upload(app, bad, tmp)
        cats = [c for c, _ in msgs]
        print(f"{bad:22} ->", msgs[-1][1][:90])
        assert "danger" in cats, bad
        assert not os.path.exists(os.path.join(tmp, bad))

    # the landing page must offer the new panel
    with app.test_request_context("/admin/"):
        g.current_steam_id = OWNER
        g.session_id = "test"
        html = routes.index()
    assert "TopDown" in html and "/admin/topdown" in html
    print("admin landing page links the TopDown panel")

    # a server without AI lines must not offer the box
    ctx = routes._upload_context("hotlapping")
    print("hotlapping upload kinds:", ctx["upload_kinds"])
    assert "ai_line" not in ctx["upload_kinds"]
    print("topdown upload kinds:", routes._upload_context("topdown")["upload_kinds"])

    shutil.rmtree(tmp)
    print("\nOK")


if __name__ == "__main__":
    main()

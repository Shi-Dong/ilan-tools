"""Tests for the web app: asset resolution and the routes that serve it."""

from __future__ import annotations

import http.client
import json
import struct

from ilan import web
from ilan.models import CANCEL_MESSAGE, TAP_MESSAGE
from ilan.server import IlanServer

# The web routes need a live server, and test_server.py already owns the
# fixture that starts one with a stubbed-out runner.
from tests.test_server import ilan_server  # noqa: F401


def _raw(server: IlanServer, path: str) -> http.client.HTTPResponse:
    """GET *path* verbatim, without following redirects or normalising the path.

    ``urlopen`` would collapse ``..`` segments client-side and silently follow
    the 302, which are exactly the two behaviours these tests need to observe.
    """
    port = server._test_port  # type: ignore[attr-defined]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    return conn.getresponse()


# ── read_asset ──────────────────────────────────────────────────────────

def test_read_asset_returns_bytes_for_a_real_asset():
    assert web.read_asset("index.html").startswith(b"<!DOCTYPE html>")


def test_read_asset_rejects_parent_traversal():
    assert web.read_asset("../server.py") is None
    assert web.read_asset("../../ilan/server.py") is None


def test_read_asset_rejects_absolute_path():
    assert web.read_asset("/etc/hosts") is None


def test_read_asset_returns_none_for_missing_file():
    assert web.read_asset("nope.js") is None


def test_read_asset_returns_none_for_a_directory():
    assert web.read_asset(".") is None


def test_content_type_known_and_unknown():
    assert web.content_type("app.css") == "text/css; charset=utf-8"
    assert web.content_type("icon-180.png") == "image/png"
    assert web.content_type("m.webmanifest") == "application/manifest+json"
    assert web.content_type("notes.txt") == web.DEFAULT_CONTENT_TYPE


# ── routes ──────────────────────────────────────────────────────────────

def test_root_redirects_to_app(ilan_server: IlanServer):
    resp = _raw(ilan_server, "/")
    assert resp.status == 302
    assert resp.getheader("Location") == "/app/"


def test_app_index_serves_html(ilan_server: IlanServer):
    resp = _raw(ilan_server, "/app/")
    body = resp.read()
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "text/html; charset=utf-8"
    assert b"<title>ilan</title>" in body


def test_app_bare_path_also_serves_index(ilan_server: IlanServer):
    assert _raw(ilan_server, "/app").status == 200


def test_app_serves_script_and_style(ilan_server: IlanServer):
    js = _raw(ilan_server, "/app/app.js")
    assert js.status == 200
    assert js.getheader("Content-Type") == "text/javascript; charset=utf-8"
    assert b"ilan web app" in js.read()

    css = _raw(ilan_server, "/app/app.css")
    assert css.status == 200
    assert css.getheader("Content-Type") == "text/css; charset=utf-8"


def test_app_serves_the_markdown_renderer(ilan_server: IlanServer):
    # app.js takes its escape helper from markdown.js at load time, so a
    # missing renderer breaks the whole page rather than just its formatting.
    js = _raw(ilan_server, "/app/markdown.js")
    assert js.status == 200
    assert js.getheader("Content-Type") == "text/javascript; charset=utf-8"
    assert b"MD" in js.read()


def test_index_loads_markdown_before_app(ilan_server: IlanServer):
    """Order matters: app.js reads MD.escapeHtml while it is being evaluated."""
    body = _raw(ilan_server, "/app/").read().decode()
    assert body.index("markdown.js") < body.index("app.js")


def test_app_serves_manifest_and_png_icon(ilan_server: IlanServer):
    manifest = _raw(ilan_server, "/app/manifest.webmanifest")
    assert manifest.status == 200
    assert manifest.getheader("Content-Type") == "application/manifest+json"
    assert json.loads(manifest.read())["short_name"] == "ilan"

    for name, side in (("icon-180.png", 180), ("icon-512.png", 512)):
        icon = _raw(ilan_server, f"/app/{name}")
        assert icon.status == 200, name
        # An apple-touch-icon served as application/octet-stream is the kind of
        # thing iOS declines silently, so pin the type as well as the status.
        assert icon.getheader("Content-Type") == "image/png", name
        data = icon.read()
        # iOS drops a home-screen icon that is not a real PNG, so assert the
        # magic bytes rather than just a 200 — and read the dimensions out of
        # the IHDR chunk, since a wrongly sized icon still passes every other
        # check here.
        assert data.startswith(b"\x89PNG\r\n\x1a\n"), name
        width, height = struct.unpack(">II", data[16:24])
        assert (width, height) == (side, side), f"{name} is {width}x{height}"


def test_every_manifest_icon_exists_and_matches_its_declared_size():
    """The manifest is hand-maintained, so it can drift from what ships.

    A missing or mis-declared icon is invisible until a phone quietly falls
    back to a screenshot of the page, which is not something a status code
    would ever reveal.
    """
    manifest = json.loads(web.read_asset("manifest.webmanifest"))
    assert manifest["icons"], "manifest declares no icons"

    for entry in manifest["icons"]:
        data = web.read_asset(entry["src"])
        assert data is not None, f"{entry['src']} is declared but not shipped"
        assert data.startswith(b"\x89PNG\r\n\x1a\n"), entry["src"]
        assert entry["type"] == "image/png", entry["src"]
        width, height = struct.unpack(">II", data[16:24])
        assert f"{width}x{height}" == entry["sizes"], (
            f"{entry['src']} is {width}x{height} but declares {entry['sizes']}"
        )


def test_apple_touch_icon_is_declared_and_shipped():
    """iOS reads the home-screen icon from this link tag, not the manifest."""
    index = web.read_asset("index.html").decode()
    assert 'rel="apple-touch-icon" href="icon-180.png"' in index
    assert web.read_asset("icon-180.png") is not None


def test_app_assets_are_revalidated(ilan_server: IlanServer):
    # A cached asset must not outlive an upgrade of the server serving it.
    assert _raw(ilan_server, "/app/app.js").getheader("Cache-Control") == "no-cache"


def test_unknown_asset_is_404(ilan_server: IlanServer):
    resp = _raw(ilan_server, "/app/does-not-exist.js")
    assert resp.status == 404


def test_traversal_via_url_is_404(ilan_server: IlanServer):
    for path in ("/app/../server.py", "/app/../../ilan/server.py"):
        resp = _raw(ilan_server, path)
        body = resp.read()
        assert resp.status == 404, path
        assert b"IlanServer" not in body, path


# ── canned messages ─────────────────────────────────────────────────────

def test_canned_messages_match_the_shared_constants(ilan_server: IlanServer):
    resp = _raw(ilan_server, "/canned-messages")
    assert resp.status == 200
    payload = json.loads(resp.read())
    # The web app's tap/cancel buttons must send byte-identical text to what
    # `ilan tap` / `ilan cancel` send, or the two front ends diverge.
    assert payload == {"tap": TAP_MESSAGE, "cancel": CANCEL_MESSAGE}

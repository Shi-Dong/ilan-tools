"""Tests for the web app: asset resolution and the routes that serve it."""

from __future__ import annotations

import http.client
import json
import re
import struct

from ilan import web
from ilan.models import (
    AGENT_IN_LOOP_LABEL,
    CANCEL_MESSAGE,
    TAP_MESSAGE,
    TaskStatus,
)
from ilan.server import ROUTES, IlanServer

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


def test_engine_colour_classes_are_defined_in_both_schemes():
    """The colour cue spans two files, so it can half-break silently.

    app.js picks a class name and app.css colours it; rename one and the task
    name simply renders in the default colour, which no status code or smoke
    test would ever notice. Both schemes must define both variables too — a
    variable declared only in the light block leaves dark mode uncoloured.
    """
    js = web.read_asset("app.js").decode()
    css = web.read_asset("app.css").decode()

    classes = set(re.findall(r"'(engine-[a-z]+)'", js))
    assert classes == {"engine-claude", "engine-codex"}, classes

    for name in classes:
        assert f".{name} {{" in css, f"{name} is used by app.js but not styled"

    # Once in :root (light) and once in the prefers-color-scheme: dark block.
    for var in ("--engine-claude", "--engine-codex"):
        assert css.count(f"{var}:") == 2, f"{var} is not defined for both schemes"


def test_every_task_status_has_a_border_colour():
    """A status with no ``.rs-*`` rule silently falls back to a neutral border.

    Nothing else would notice: the card still renders, just without the cue.
    Deriving the expected set from ``TaskStatus`` rather than listing it here
    means adding a status to the enum without giving the web app a colour for
    it fails this test.
    """
    css = web.read_asset("app.css").decode()

    # The statuses the list can actually display: the stored ones, plus the
    # AGENT_IN_LOOP label that display_status() substitutes for two of them.
    displayed = {s.value for s in TaskStatus} | {AGENT_IN_LOOP_LABEL}

    mapping = dict(re.findall(
        r"\.rs-([A-Z_]+)\s*\{\s*--row-status:\s*var\((--st-[a-z-]+)\)", css,
    ))
    assert set(mapping) == displayed, (
        f"missing: {displayed - set(mapping)}; extra: {set(mapping) - displayed}"
    )

    # Each border colour must exist in both schemes, or one mode loses the cue.
    for status, var in mapping.items():
        assert css.count(f"{var}:") == 2, (
            f"{status} uses {var}, which is not defined for both colour schemes"
        )


def test_the_card_border_is_thick_enough_to_read_its_colour():
    """A hairline border carries the hue but not legibly.

    At 1px the status colour was present and still hard to tell apart at arm's
    length, worst on the muted DONE and DISCARDED tones. Nothing else would
    catch a revert to 1px: the colour would still be correct, just unreadable.
    """
    css = web.read_asset("app.css").decode()
    match = re.search(
        r"\.card \{[^}]*?border:\s*(\d+)px solid var\(--row-status", css, re.S,
    )
    assert match, "the card no longer takes its border colour from --row-status"
    assert int(match.group(1)) >= 2, (
        f"card border is {match.group(1)}px; 1px is not legible enough to "
        "distinguish the status colours"
    )


def _relative_luminance(colour: str) -> float:
    """WCAG relative luminance of a ``#rrggbb`` string."""
    channels = (int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    linear = [
        c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    la, lb = _relative_luminance(a), _relative_luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def test_the_done_button_is_filled_with_its_own_green():
    """The card's Done button takes its colour from --done, not from a status.

    Reusing --st-finished would tie a control's fill to a status colour, so a
    later tweak to how AGENT_FINISHED reads on a card would silently restyle a
    button in the same edit.
    """
    css = web.read_asset("app.css").decode()
    rule = re.search(r"\.btn-done \{(.*?)\}", css, re.S)
    assert rule, "the Done button has no fill rule"
    assert "background: var(--done)" in rule.group(1)
    assert "color: var(--done-contrast)" in rule.group(1)


def test_the_done_button_label_is_legible_in_both_colour_schemes():
    """A filled button carries a label, so the pair has to clear 4.5:1.

    The green it echoes, --st-finished, only ever colours text sitting on a
    card and is too light to carry white text at this size. Nothing else would
    catch the difference: the button would still render, still be green, and
    simply be hard to read — worst in whichever scheme was not being looked at
    when the colour was chosen.
    """
    css = web.read_asset("app.css").decode()
    pairs = re.findall(
        r"--done:\s*(#[0-9a-f]{6});\s*\n\s*--done-contrast:\s*(#[0-9a-f]{6});", css,
    )
    assert len(pairs) == 2, (
        f"expected --done/--done-contrast in both schemes, found {len(pairs)}"
    )
    for fill, label in pairs:
        assert _contrast(fill, label) >= 4.5, (
            f"the Done label is {_contrast(fill, label):.2f}:1 on {fill}"
        )

    # It also has to separate from the card it sits on, in both schemes; a
    # filled control needs 3:1 against its background to read as a control.
    for fill, card in zip([p[0] for p in pairs], ("#ffffff", "#1c1c1e")):
        assert _contrast(fill, card) >= 3.0, (
            f"the Done fill {fill} is {_contrast(fill, card):.2f}:1 on {card}"
        )


def test_the_card_actions_row_tightens_its_buttons():
    """Three buttons share one row at phone width, and only just fit.

    At the shared 14px of button padding, "Show Details" wraps onto a second
    line and the whole row goes ragged. This is a stand-in for a measurement
    the test suite cannot take — there is no layout engine here — so it guards
    the override rather than the wrapping itself.
    """
    css = web.read_asset("app.css").decode()
    rule = re.search(r"\.row-actions \.btn \{(.*?)\}", css, re.S)
    assert rule, "the card action buttons no longer have a sizing rule"
    padding = re.search(r"padding:\s*0\s+(\d+)px", rule.group(1))
    assert padding, "the row no longer overrides the default button padding"
    assert int(padding.group(1)) <= 8, (
        f"{padding.group(1)}px of padding wraps the middle label at 390px"
    )


def test_the_done_button_posts_to_a_route_the_server_serves():
    """A renamed route would leave the button posting into a 404."""
    js = web.read_asset("app.js").decode()
    assert "/done`" in js, "the card's Done button no longer posts to /done"
    assert (
        "POST", r"^/tasks/([^/]+)/done$", "handle_task_done",
    ) in ROUTES


def test_web_app_does_not_show_task_cost():
    """Cost is deliberately absent from the web UI.

    The API still returns ``cost_usd`` and ``ilan ls`` still shows it — this is
    a front-end decision, not a data change. Asserting it here means a future
    edit that reintroduces a cost field is caught in review rather than
    quietly appearing on a phone screen.
    """
    js = web.read_asset("app.js").decode()
    for token in ("cost_usd", "fmtCost"):
        assert token not in js, f"{token} is back in the web app"


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

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


def _scheme_values(css: str) -> tuple[dict[str, str], dict[str, str]]:
    """The custom properties as light mode sets them, and as dark mode does.

    Dark mode only overrides some of them, so its map starts from light's and
    is updated — which is what the cascade does.
    """
    root = re.search(r":root \{(.*?)\n\}", css, re.S)
    dark = re.search(r"@media \(prefers-color-scheme: dark\) \{(.*?)\n  \}", css, re.S)
    assert root and dark, "the colour schemes are no longer both declared"

    read = lambda block: dict(re.findall(r"(--[a-z-]+):\s*(#[0-9a-f]{3,8});", block))  # noqa: E731
    light = read(root.group(1))
    return light, {**light, **read(dark.group(1))}


def _status_fills(css: str) -> dict[str, str]:
    """Status name → the ``--st-*`` variable its cue is painted with."""
    return dict(re.findall(
        r"\.rs-([A-Z_]+)\s*\{\s*--row-status:\s*var\((--st-[a-z-]+)\)", css,
    ))


def test_the_status_rail_reads_as_a_shape_in_both_schemes():
    """The leading edge is a block of colour, so it needs 3:1 against the card.

    It is not text, so 3:1 is the bar rather than 4.5:1 — but every status has
    to clear it, including the muted pair the old thin border served worst.
    Computed from the palette, so a new status or a retuned colour is checked
    rather than assumed.
    """
    css = web.read_asset("app.css").decode()
    light, dark = _scheme_values(css)

    for scheme, values in (("light", light), ("dark", dark)):
        card = values["--bg-elevated"]
        for status, var in _status_fills(css).items():
            fill = values[var]
            assert _contrast(fill, card) >= 3.0, (
                f"{scheme}: the {status} rail is {fill} at "
                f"{_contrast(fill, card):.2f}:1 on {card}"
            )


def test_every_status_pill_can_be_read_in_both_schemes():
    """The pill is a filled shape carrying a label, so the label needs 4.5:1.

    The ink defaults to the card's own colour, which is right almost
    everywhere: light mode's card is white and its status fills are dark, dark
    mode's is near-black and its fills are light. The exceptions are declared
    as ``--row-ink`` overrides, and this resolves whichever applies rather than
    trusting that the defaults happen to work — the status palette was tuned
    for *text on a card*, which is exactly the luminance range where neither a
    white nor a black label is safe.
    """
    css = web.read_asset("app.css").decode()
    light, dark = _scheme_values(css)

    overridden = {}
    for selectors, ink in re.findall(
        r"^((?:\.rs-[A-Z_]+,?\s*)+)\{\s*--row-ink:\s*(#[0-9a-f]{6});", css, re.M,
    ):
        for status in re.findall(r"\.rs-([A-Z_]+)", selectors):
            overridden[status] = ink

    for scheme, values in (("light", light), ("dark", dark)):
        for status, var in _status_fills(css).items():
            fill = values[var]
            ink = overridden.get(status, values["--bg-elevated"])
            assert _contrast(fill, ink) >= 4.5, (
                f"{scheme}: the {status} pill is {ink} on {fill}, "
                f"only {_contrast(fill, ink):.2f}:1"
            )


def test_the_status_pill_is_one_rule_serving_both_surfaces():
    """The list and the conversation show the status as the same pill.

    They used to differ — a pill on a card, plain coloured text in the header —
    which meant the same fact had two shapes and the eye had to find it twice.
    Sameness here is structural rather than copied: the header reuses the
    card's own ``.row-meta`` container, so a single rule styles both and there
    is no second selector to keep in step.

    The rule still has to out-specify the ``.st-*`` colour it replaces, which a
    single class would not, so a bare ``.status`` must stay unfilled.
    """
    css = web.read_asset("app.css").decode()
    js = web.read_asset("app.js").decode()

    rule = re.search(r"\.row-meta \.status \{(.*?)\}", css, re.S)
    assert rule, "the status pill rule is gone"
    assert "background: var(--row-status" in rule.group(1)
    assert "border-radius: 999px" in rule.group(1)

    bare = re.search(r"^\.status \{(.*?)\}", css, re.S | re.M)
    assert bare, ".status is not styled at all"
    assert "background:" not in bare.group(1), (
        "a bare .status fill would beat --row-status on source order in one of "
        "the two places, and only in one of them"
    )

    # Built in one place, so the two surfaces cannot drift into near-identical.
    assert js.count('<span class="status') == 1, (
        "the status span is constructed in more than one place"
    )
    calls = js.count("statusPill(task)") - js.count("function statusPill(task)")
    assert calls == 2, (
        f"the shared status pill has {calls} callers, not the list and the "
        "conversation"
    )


def test_both_surfaces_carry_what_the_pill_needs_to_be_coloured():
    """--row-status comes from an rs-* class, and the pill is silent without it.

    Missing it renders a pill in the plain border grey rather than throwing, so
    every container holding a pill has to set it. Both are checked here because
    the failure looks like a styling choice rather than a bug.
    """
    js = web.read_asset("app.js").decode()

    holders = re.findall(r'class="([^"]*\brow-meta\b[^"]*)"', js)
    assert len(holders) == 2, f"expected two .row-meta containers, found {holders}"

    # The card sets rs-* on the card itself rather than on its meta row, so the
    # class is looked for on the element or on a container in the same template.
    assert re.search(r'<div class="card rs-\$\{esc\(status\)\}', js), (
        "the card no longer sets the status class the pill reads"
    )
    assert re.search(r'class="hdr-sub row-meta rs-\$\{esc\(status\)\}', js), (
        "the conversation header no longer sets the status class the pill reads"
    )


def test_the_conversation_header_does_not_name_the_backend():
    """Asked for, and the information is not lost with the word.

    The task name in the title above is already coloured by backend, the same
    as in the list, so the line was spending width to repeat in a word what the
    colour was saying. The ••• sheet still names it, on the entry that changes
    it — which is why this checks the sub-line rather than the file.
    """
    js = web.read_asset("app.js").decode()

    sub = re.search(r"const sub = \[(.*?)\]", js, re.S)
    assert sub, "the conversation sub-line is gone"
    assert "task.engine" not in sub.group(1), (
        f"the backend is still on the sub-line: {sub.group(1).strip()}"
    )
    assert "task.model" in sub.group(1), (
        "the model went with it — the line should keep everything else"
    )

    # The compensating signal, and the reason removing the word is not a loss.
    assert "engineClass(task)" in js, (
        "nothing colours the task name by backend any more"
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

    # The leading edge is widened into a rail. An outline of any thickness is
    # four thin lines the eye reads as the shape of the card; one wide edge is
    # a block of colour that lines up down the list.
    rail = re.search(r"\.card \{[^}]*?border-left-width:\s*(\d+)px", css, re.S)
    assert rail, "the card's leading edge is no longer widened into a rail"
    assert int(rail.group(1)) >= int(match.group(1)) * 4, (
        f"the rail is {rail.group(1)}px against a {match.group(1)}px outline, "
        "which is not enough to read as a different thing"
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


def test_each_quiet_action_takes_its_ink_from_its_own_variable():
    """Tap and Done colour themselves, not from a status.

    Reusing a status colour would tie a control to one, so a later tweak to how
    AGENT_FINISHED reads on a card would silently restyle a button in the same
    edit. --act-done is deliberately not --st-done, which is the DONE *status*
    and an unrelated colour.
    """
    css = web.read_asset("app.css").decode()
    for selector, var in ((".act-tap", "--act-tap"), (".act-done", "--act-done")):
        rule = re.search(rf"\{selector} \{{(.*?)\}}", css, re.S)
        assert rule, f"no rule for {selector}"
        assert f"color: var({var})" in rule.group(1), (
            f"{selector} no longer takes its ink from {var}"
        )


def test_every_quiet_action_is_legible_on_the_card():
    """These colours are ink now, which is a stricter test than being a fill.

    As fills they only had to carry their own label. As text on the card they
    need 4.5:1 against the card itself, in both schemes — and the amber that
    served as a fill does not: #a56800 was chosen to sit *under* white text and
    manages 3.71:1 as text on a dark card.

    Resolved per scheme rather than counted, so a colour that works in both can
    be declared once and inherited by the dark block.
    """
    css = web.read_asset("app.css").decode()
    light, dark = _scheme_values(css)

    for var in ("--act-tap", "--act-done"):
        for scheme, values in (("light", light), ("dark", dark)):
            ink, card = values[var], values["--bg-elevated"]
            assert _contrast(ink, card) >= 4.5, (
                f"{scheme}: {var} is {ink} on {card}, "
                f"only {_contrast(ink, card):.2f}:1"
            )


def test_exactly_one_card_action_is_filled():
    """The hierarchy is the point of the design, so it is asserted directly.

    Three filled buttons is what this replaced: with everything shouting, the
    card's own status colour — the only one of them carrying information — lost
    to its own buttons. Colour is spent once, on the way into the task.

    Counted rather than named, so filling a second one fails here even if the
    one that is meant to lead still leads.
    """
    css = web.read_asset("app.css").decode()

    filled = []
    for selector in (".act-tap", ".act-done", ".act-details"):
        rule = re.search(rf"\{selector} \{{(.*?)\}}", css, re.S)
        assert rule, f"no rule for {selector}"
        background = re.search(r"background:\s*([^;]+);", rule.group(1))
        if background and background.group(1).strip() not in ("none", "transparent"):
            filled.append(selector)

    assert filled == [".act-details"], (
        f"expected only the way into the task to be filled, found {filled}"
    )


def test_the_leading_action_reads_as_a_control():
    """The filled one is identified by its fill, so the fill has to carry it.

    Two separate bars: 3:1 against the card for the fill to register as a
    control at all, and 4.5:1 for the label sitting on it.
    """
    css = web.read_asset("app.css").decode()
    light, dark = _scheme_values(css)

    rule = re.search(r"\.act-details \{(.*?)\}", css, re.S)
    assert rule, "the leading action has no rule"
    assert "background: var(--accent)" in rule.group(1)

    for scheme, values in (("light", light), ("dark", dark)):
        fill, card, label = (
            values["--accent"], values["--bg-elevated"], values["--accent-contrast"],
        )
        assert _contrast(fill, card) >= 3.0, (
            f"{scheme}: the fill is {_contrast(fill, card):.2f}:1 on the card"
        )
        assert _contrast(fill, label) >= 4.5, (
            f"{scheme}: its label is {_contrast(fill, label):.2f}:1 on it"
        )


def test_the_quiet_actions_are_bounded_as_visibly_as_anything_else_in_the_app():
    """Their hairline is tinted and low-contrast, which is deliberate.

    It is not what identifies them — the text label and the glyph do that, both
    at 4.5:1 — so this is not held to the 3:1 that an edge carrying meaning on
    its own would need. What it is held to is the app's own standard: the same
    hairline every field, card and button already draws. Measured against
    --border rather than against an absolute, so the comparison is with a bar
    the app has already accepted everywhere else.
    """
    css = web.read_asset("app.css").decode()
    light, dark = _scheme_values(css)

    for selector, var in ((".act-tap", "--act-tap"), (".act-done", "--act-done")):
        rule = re.search(rf"\{selector} \{{(.*?)\}}", css, re.S)
        assert rule, f"no rule for {selector}"
        mix = re.search(
            rf"border-color:\s*color-mix\(in srgb, var\({var}\) (\d+)%, transparent\)",
            rule.group(1),
        )
        assert mix, f"{selector} no longer draws a hairline tinted with {var}"

        pct = int(mix.group(1)) / 100
        for scheme, values in (("light", light), ("dark", dark)):
            card = values["--bg-elevated"]
            edge = _mix(values[var], card, pct)
            plain = _contrast(values["--border"], card)
            assert _contrast(edge, card) >= plain, (
                f"{scheme}: {selector}'s edge is {_contrast(edge, card):.2f}:1 on "
                f"the card, fainter than the app's own border at {plain:.2f}:1"
            )


def test_every_card_action_is_a_full_size_tap_target():
    """These were 36px, under the 44px minimum the rest of the app keeps.

    Small buttons in a row are the ones a thumb misses, and this row is the
    only place in the app that had any.
    """
    css = web.read_asset("app.css").decode()
    rule = re.search(r"\n\.act \{(.*?)\}", css, re.S)
    assert rule, "the card actions have no sizing rule"
    assert "min-height: var(--tap)" in rule.group(1), (
        "the card actions no longer claim the shared 44px minimum"
    )


def test_every_glyph_resolves_to_a_symbol_in_the_sprite():
    """A <use> pointing at nothing renders nothing, and throws nothing.

    The button would still be there, still be tappable, and simply have lost
    its icon — so nothing else in the suite would notice. Both directions are
    checked: every reference resolves, and no symbol is left unused.
    """
    js = web.read_asset("app.js").decode()
    html = web.read_asset("index.html").decode()

    defined = set(re.findall(r'<symbol id="([^"]+)"', html))
    assert defined, "the icon sprite defines no symbols"

    keys = re.search(r"const ICONS = \{(.*?)\};", js, re.S)
    assert keys, "app.js no longer names its glyphs"
    referenced = set(re.findall(r"'([^']+)'", keys.group(1)))
    assert referenced, "the glyph table is empty"

    assert referenced <= defined, (
        f"these glyphs are used but not defined: {sorted(referenced - defined)}"
    )
    assert defined <= referenced, (
        f"these symbols are defined but never used: {sorted(defined - referenced)}"
    )

    assert '<use href="#${ICONS[name]}">' in js, (
        "the glyphs are no longer pulled from the sprite by id"
    )


def test_the_card_buttons_run_tap_done_details():
    """The two that act on the task sit together; the one that leaves is last."""
    js = web.read_asset("app.js").decode()
    row = re.search(r'<div class="row-actions">(.*?)</div>', js, re.S)
    assert row, "the card no longer has an actions row"
    order = re.findall(r"data-(tap|done|details)=", row.group(1))
    assert order == ["tap", "done", "details"], order


def test_a_closed_card_offers_only_show_details():
    """Neither Tap nor Done means anything once a task is closed.

    There is no agent left to tap — the message would go to a task whose agent
    has stopped — and closing something already DONE is not an action worth
    offering. The actions sheet has always applied that rule to both, so the
    card does too, through a single condition rather than one per button: two
    conditions is how they drift apart, which is exactly what happened when
    Done was gated and Tap was not.
    """
    js = web.read_asset("app.js").decode()
    row = re.search(r'<div class="row-actions">(.*?)</div>', js, re.S)
    assert row, "the card no longer has an actions row"

    gated = re.search(
        r"\$\{TERMINAL_STATUSES\.has\(task\.status\) \? '' : `(.*?)`\}",
        row.group(1), re.S,
    )
    assert gated, "the card's actions are no longer gated on the task being open"

    assert "data-tap=" in gated.group(1), "Tap is offered on a closed task"
    assert "data-done=" in gated.group(1), "Done is offered on a closed task"
    assert "data-details=" not in gated.group(1), (
        "Show Details is gated too, leaving a closed card with no actions at all"
    )


def test_the_amber_stays_off_the_backend_colour_it_sits_beside():
    """Tap's amber and the Claude task-name amber appear in the same card.

    The dark-mode value had to move — the fill it inherited reads 3.71:1 as
    text — and the obvious lighter amber to reach for is --engine-claude, which
    already colours task names two lines above the button. Landing on it would
    make the button read as another Claude-coloured word.
    """
    css = web.read_asset("app.css").decode()
    light, dark = _scheme_values(css)

    for scheme, values in (("light", light), ("dark", dark)):
        assert values["--act-tap"] != values["--engine-claude"], (
            f"{scheme}: Tap's ink is the backend colour, {values['--act-tap']}"
        )


def test_a_hidden_ask_bar_is_really_hidden():
    """``.ask-bar`` is display:flex, which beats the ``hidden`` attribute.

    Without an explicit rule the bar would be permanently on screen offering to
    quote nothing. Nothing in the Python suite renders CSS, so assert the rule
    itself; the browser check that catches it for real needs a browser.
    """
    css = web.read_asset("app.css").decode()
    assert re.search(r"\.ask-bar\[hidden\]\s*\{\s*display:\s*none", css), (
        "a hidden ask bar would still be laid out"
    )


def test_the_clear_button_sits_inside_the_message_box():
    """It overlays the box's corner rather than taking a slot in the row.

    Three declarations make that work together, and any one of them alone is
    the wrong layout: the wrapper establishes the positioning context, the
    button is taken out of flow, and the box reserves room on its right so the
    text does not run underneath. There is no layout engine in the Python
    suite, so the declarations are asserted; a browser checks the geometry.
    """
    css = web.read_asset("app.css").decode()
    js = web.read_asset("app.js").decode()

    assert '<div class="composer-field">' in js, "the box is no longer wrapped"

    field = re.search(r"\.composer-field \{(.*?)\}", css, re.S)
    assert field, "the field wrapper is not styled"
    assert "position: relative" in field.group(1), (
        "without a positioning context the button escapes to the page corner"
    )

    button = re.search(r"^\.btn-clear \{(.*?)\}", css, re.S | re.M)
    assert button, "the clear button is not styled"
    assert "position: absolute" in button.group(1)

    textarea = re.search(r"\.composer textarea \{(.*?)\}", css, re.S)
    assert textarea, "the message box is not styled"
    assert "padding-right" in textarea.group(1), (
        "text would run underneath the clear button"
    )


def test_a_disabled_primary_button_goes_grey_rather_than_faint():
    """Fading the accent leaves a pale blue, which still reads as the blue button.

    The generic disabled rule only drops opacity; over the composer's backdrop
    that renders the accent as roughly #8ab2e2 — recognisably "the blue button,
    badly printed" — which invites the tap it is about to refuse. A neutral fill
    says unavailable. The rule has to come after the generic one: both are a
    class plus an attribute, so source order alone decides which wins.
    """
    css = web.read_asset("app.css").decode()

    rule = re.search(r"\.btn-primary\[disabled\] \{(.*?)\}", css, re.S)
    assert rule, "a disabled primary button is still only faded"
    body = rule.group(1)
    assert "background: var(--bg-sunken)" in body
    assert "color: var(--text-dim)" in body
    assert "opacity: 1" in body, (
        "without this the neutral fill is faded on top of being neutral"
    )
    assert css.index(".btn-primary[disabled]") > css.index(".btn[disabled]"), (
        "the generic faded rule wins on source order, so the button stays blue"
    )


def test_the_composer_ships_both_buttons_already_disabled():
    """The markup's own default has to be the safe one.

    A freshly rendered composer is always empty, so neither button should be
    live in the HTML that produces it. ``syncComposer`` sets them a moment
    later either way, which is exactly why this is worth pinning: nothing about
    the rendered result would change if the attributes were dropped, right up
    until that call moves behind an await or throws, and then the first frame
    offers a blue Send over an empty box.
    """
    js = web.read_asset("app.js").decode()

    for control in ('id="send"', 'id="clear-reply"'):
        tag = re.search(rf"<button[^>]*{re.escape(control)}[^>]*>", js)
        assert tag, f"{control} is no longer rendered"
        assert "disabled" in tag.group(0), (
            f"{control} ships enabled over an empty box"
        )


def test_the_composer_buttons_share_one_test_for_an_empty_draft():
    """Send and clear must agree on what counts as nothing.

    Whitespace is not a message and is not worth clearing, so both turn on the
    trimmed value. Two separate conditions would be free to drift — which is
    exactly how Tap and Done came apart on the card.
    """
    js = web.read_asset("app.js").decode()

    sync = re.search(r"const syncComposer = \(\) => \{(.*?)\n    \};", js, re.S)
    assert sync, "the composer no longer derives its state in one place"
    body = sync.group(1)

    assert body.count("replyBox.value.trim()") == 1, (
        "the emptiness test is computed more than once, so the buttons can disagree"
    )
    assert "clearBtn.disabled = !hasDraft" in body
    assert "sendBtn.disabled = !hasDraft" in body


def test_the_clear_button_is_out_of_sight_while_there_is_nothing_to_clear():
    """A text field's clear control appears only once there is text.

    It is hidden rather than merely dimmed, which is only affordable because it
    is positioned: as a sibling in the row this would have shifted the layout
    on the first keystroke. The rule has to come after the generic disabled
    rule, since both are one class plus one attribute — equal specificity, so
    source order decides which opacity wins.
    """
    css = web.read_asset("app.css").decode()

    rule = re.search(r"\.btn-clear\[disabled\] \{(.*?)\}", css, re.S)
    assert rule, "a disabled clear button is not hidden"
    assert "opacity: 0" in rule.group(1)
    assert "pointer-events: none" in rule.group(1), (
        "an invisible button that still takes taps is worse than a visible one"
    )
    assert css.index(".btn-clear[disabled]") > css.index(".btn[disabled]"), (
        "the generic disabled rule wins on source order, so the button stays dimly visible"
    )


def test_the_ask_bar_and_composer_dock_together():
    """They share one sticky container so the bar stacks above the composer.

    Sticky on each separately would let the bar slide underneath when it
    appears, which puts it behind the thing it is supposed to sit above.
    """
    css = web.read_asset("app.css").decode()
    js = web.read_asset("app.js").decode()

    dock = re.search(r"\.dock \{(.*?)\}", css, re.S)
    assert dock, "the composer no longer has a docking container"
    assert "position: sticky" in dock.group(1)
    assert "bottom: 0" in dock.group(1)

    composer = re.search(r"^\.composer \{(.*?)\}", css, re.S | re.M)
    assert composer, ".composer is not styled"
    assert "position: sticky" not in composer.group(1), (
        "the composer sticks on its own again, which lets the ask bar slide under it"
    )
    assert '<div class="dock">' in js


def test_the_ask_bar_only_quotes_from_message_bodies():
    """Both ends of the selection are checked, not just where it started."""
    js = web.read_asset("app.js").decode()
    assert "inMessage(sel.anchorNode) && inMessage(sel.focusNode)" in js, (
        "a selection running out of a message would be quoted anyway"
    )


def test_the_web_app_carries_no_inline_styles():
    """Styling belongs in the stylesheet, where the schemes can both reach it.

    An inline rule cannot be overridden by the dark-mode block and does not
    show up in any of the layout assertions here, so it is the one place a
    sizing mistake can hide.
    """
    js = web.read_asset("app.js").decode()
    assert 'style="' not in js, "a style attribute is back in the markup"


def test_the_settings_checkbox_is_big_enough_to_hit():
    """The browser default is a ~13px square, well under the 44px guidance.

    This used to be an inline style; moving it to the stylesheet is what makes
    it assertable at all.
    """
    css = web.read_asset("app.css").decode()
    rule = re.search(r"\.checkbox \{(.*?)\}", css, re.S)
    assert rule, "the checkbox has no sizing rule"
    sizes = [int(v) for v in re.findall(r"(?:width|height):\s*(\d+)px", rule.group(1))]
    assert len(sizes) == 2, f"expected a width and a height, got {sizes}"
    assert all(size >= 24 for size in sizes), f"checkbox is {sizes}px"


def test_the_card_actions_row_tightens_its_buttons():
    """Three buttons share one row at phone width, and only just fit.

    Each now carries a glyph as well as a label, so the row is tighter than it
    was even with the shorter middle label. This is a stand-in for a
    measurement the test suite cannot take — there is no layout engine here —
    so it guards the padding rather than the wrapping itself. The real
    measurement is taken in a browser, against main, in the PR.
    """
    css = web.read_asset("app.css").decode()
    rule = re.search(r"\n\.act \{(.*?)\}", css, re.S)
    assert rule, "the card action buttons no longer have a sizing rule"
    padding = re.search(r"padding:\s*0\s+(\d+)px", rule.group(1))
    assert padding, "the row no longer sets its button padding"
    assert int(padding.group(1)) <= 8, (
        f"{padding.group(1)}px of padding wraps a label at 390px"
    )


def test_the_done_button_posts_to_a_route_the_server_serves():
    """A renamed route would leave the button posting into a 404."""
    js = web.read_asset("app.js").decode()
    assert "/done`" in js, "the card's Done button no longer posts to /done"
    assert (
        "POST", r"^/tasks/([^/]+)/done$", "handle_task_done",
    ) in ROUTES


def test_the_back_control_is_big_enough_to_aim_at():
    """A single narrow glyph is the one mark that can be too small to hit.

    Its tap target was already the 44px minimum when it was still hard to use —
    the target was fine, the glyph was not. Nothing behavioural would notice a
    revert: a smaller chevron still renders and still works.
    """
    js = web.read_asset("app.js").decode()
    css = web.read_asset("app.css").decode()

    backs = re.findall(r'<button class="([^"]*)" id="back"', js)
    assert backs, "no back button found"
    for classes in backs:
        assert "btn-back" in classes, f"a back button is missing btn-back: {classes}"
        # btn-sm is what made it small in the first place.
        assert "btn-sm" not in classes, f"a back button is still small: {classes}"

    rule = re.search(r"\.btn-back \{(.*?)\}", css, re.S)
    assert rule, ".btn-back is not styled"
    size = re.search(r"font-size:\s*(\d+)px", rule.group(1))
    assert size and int(size.group(1)) >= 24, (
        f"back glyph is {size.group(1) if size else 'unset'}px; too small to aim at"
    )
    assert "min-height: var(--tap)" in rule.group(1)
    assert "width: var(--tap)" in rule.group(1)


def test_the_conversation_asks_the_server_for_a_bounded_tail():
    """The reveal rule is the server's, not a second implementation here."""
    js = web.read_asset("app.js").decode()
    assert "/tail?n=${state.detailShown}" in js


def test_the_working_duration_is_measured_from_when_the_task_started_working():
    """created_at would count time spent queued or in an earlier status."""
    js = web.read_asset("app.js").decode()
    assert "secondsSince(task.status_changed_at)" in js


# ── the collapsed card ──────────────────────────────────────────────────

def test_a_collapsed_card_hides_the_summary_and_the_metadata():
    """The collapsed view is defined by CSS, so assert the rules exist.

    A collapsed card shows the pin, alias, name, unread marker and status. The
    summary, the engine and the age are what it drops, hidden by class rather
    than by a second rendering path — so losing one of these selectors would
    quietly put the detail back.
    """
    css = web.read_asset("app.css").decode()

    for selector in (".card.collapsed .row-sum", ".card.collapsed .meta-detail"):
        assert selector in css, f"{selector} is no longer hidden when collapsed"

    # The status must NOT be hidden: it is one of the things that stay, and it
    # carries the WORKING duration with it.
    assert ".card.collapsed .status" not in css

    # Nor the sleep suffix. This departs from `ls -c`, which omits it: most of
    # the list is only ever seen collapsed on a phone, and whether an agent is
    # asleep is worth knowing without expanding the card first.
    assert ".card.collapsed .sleep" not in css, (
        "the sleep suffix is hidden again when collapsed"
    )

    # Three buttons on every collapsed card would undo the density that
    # collapsing by default exists to provide.
    assert ".card.collapsed .row-actions { display: none; }" in css


def test_the_summary_is_not_truncated_on_an_expanded_card():
    """The one-liner may already have been shortened upstream.

    Clamping it here truncated a second time, cutting the tail off a sentence
    that was meant to be complete. The summary only renders on an expanded
    card, so there is nothing for a clamp to buy.
    """
    css = web.read_asset("app.css").decode()
    rule = re.search(r"\.row-sum \{(.*?)\}", css, re.S)
    assert rule, ".row-sum is not styled"
    assert "line-clamp" not in rule.group(1), "the summary is clamped again"
    assert "-webkit-box" not in rule.group(1), "the summary is clamped again"


def test_the_card_body_is_the_toggle():
    """The row itself carries the toggle, rather than a separate chevron."""
    js = web.read_asset("app.js").decode()
    assert 'data-toggle="${esc(task.name)}"' in js


# ── reopening a closed task ─────────────────────────────────────────────

def _revive_actions() -> dict[str, str]:
    """The closed status → endpoint pairs the revive button is built from."""
    js = web.read_asset("app.js").decode()
    table = re.search(r"const REVIVE_ACTIONS = \{(.*?)\n\};", js, re.S)
    assert table, "REVIVE_ACTIONS is gone; the revive button has no source"
    return dict(re.findall(r"([A-Z_]+): \{ choice: '([a-z]+)'", table.group(1)))


def test_each_closed_status_is_reopened_by_its_own_endpoint():
    """The server refuses a mismatch, so one generic button would not do.

    ``undone`` only accepts a DONE task and ``undiscard`` only a DISCARDED one,
    so a single endpoint would work for half the closed tasks and 409 for the
    rest. Deriving the expected set from ``TaskStatus`` means adding a closed
    status to the enum without giving the web app a way back fails here.
    """
    closed = {TaskStatus.DONE.value, TaskStatus.DISCARDED.value}
    assert _revive_actions() == {"DONE": "undone", "DISCARDED": "undiscard"}
    assert set(_revive_actions()) == closed


def test_the_revive_buttons_post_to_routes_the_server_actually_serves():
    """A renamed route would leave the button posting into a 404.

    Nothing else would notice: the button still renders, the tap still fires,
    and the failure only shows up as a toast on a phone.
    """
    served = {pattern for method, pattern, _ in ROUTES if method == "POST"}
    for status, choice in _revive_actions().items():
        assert rf"^/tasks/([^/]+)/{choice}$" in served, (
            f"the web app posts /{choice} for a {status} task, "
            "which the server does not route"
        )


def test_the_statuses_treated_as_closed_are_the_ones_with_a_way_back():
    """One table feeds both, so the page cannot hide the composer from a task
    it then offers no button to."""
    js = web.read_asset("app.js").decode()
    assert "new Set(Object.keys(REVIVE_ACTIONS))" in js, (
        "TERMINAL_STATUSES is no longer derived from REVIVE_ACTIONS"
    )


def test_the_revive_button_fills_the_bar_it_sits_in():
    """It is alone in a flex row, so without this it shrinks to its label."""
    css = web.read_asset("app.css").decode()
    rule = re.search(r"\.composer \.btn-revive \{(.*?)\}", css, re.S)
    assert rule, "the revive button has no layout rule in the composer bar"
    assert "flex: 1" in rule.group(1)


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


def _mix(fg: str, bg: str, pct: float) -> str:
    """*pct* of *fg* composited over *bg*, as ``color-mix`` would."""
    return "#" + "".join(
        f"{round(pct * int(fg[i:i + 2], 16) + (1 - pct) * int(bg[i:i + 2], 16)):02x}"
        for i in (1, 3, 5)
    )


def test_a_task_name_in_a_toast_is_set_as_code():
    """The toast is an inverted pill, so the message styling does not fit it.

    ``.md code`` uses a fixed light background, which on a dark pill would be
    the wrong tone entirely. The toast's chip is a wash of its own text colour
    instead, so one rule covers both schemes and the error variant.
    """
    css = web.read_asset("app.css").decode()

    rule = re.search(r"\.toast code \{(.*?)\}", css, re.S)
    assert rule, "a task name in a toast is no longer set apart"
    assert "currentColor" in rule.group(1), (
        "the chip uses a fixed colour, which cannot suit both pill and error pill"
    )
    # The toast breaks long words anywhere, so a long name splits across lines.
    # Without this the chip is one box torn in half rather than two boxes.
    assert "box-decoration-break: clone" in rule.group(1)
    assert "-webkit-box-decoration-break: clone" in rule.group(1), (
        "Safari still needs the prefix, and Safari is the target"
    )


def test_the_toast_chip_does_not_swallow_its_own_label():
    """Washing the background toward the text colour costs label contrast.

    The chip is drawn *from* the label's colour, so the stronger it is the less
    the label stands out on it. Computed for both schemes at whatever
    percentage is declared, so tuning the chip up later is checked rather than
    just allowed.

    Only the ordinary pill is checked. The error pill is a separate matter: its
    label is white on --danger, which is already 2.78:1 in dark mode before any
    chip exists, and no task name is put into an error toast.
    """
    css = web.read_asset("app.css").decode()
    light, dark = _scheme_values(css)

    rule = re.search(r"\.toast code \{(.*?)\}", css, re.S)
    assert rule, "the toast chip is not styled"
    pct = re.search(r"currentColor\s+(\d+)%", rule.group(1))
    assert pct, "the chip's strength is not declared as a percentage"
    strength = int(pct.group(1)) / 100

    for scheme, values in (("light", light), ("dark", dark)):
        # The pill inverts the page: its background is the text colour and its
        # own text is the page background.
        pill, label = values["--text"], values["--bg"]
        chip = _mix(label, pill, strength)
        assert _contrast(label, chip) >= 4.5, (
            f"{scheme}: a name on the chip is {_contrast(label, chip):.2f}:1 "
            f"({label} on {chip}) at {pct.group(1)}%"
        )


def test_the_list_header_shows_the_app_icon():
    """The page a phone opens should look like the thing that was tapped.

    The src is checked against what actually ships: a renamed or dropped icon
    does not throw, it renders a broken-image glyph, which no behavioural test
    would notice. It is also checked to be relative — an absolute path would
    bake in the mount point, which is the one thing the whole front end avoids
    so that it works unchanged behind any prefix.
    """
    js = web.read_asset("app.js").decode()

    tag = re.search(r"<img class=\"hdr-logo\"[^>]*>", js)
    assert tag, "the list header no longer shows the app icon"

    src = re.search(r'src="([^"]+)"', tag.group(0))
    assert src, "the header logo has no source"
    assert not src.group(1).startswith(("/", "http")), (
        f"{src.group(1)} is absolute, which assumes where the app is mounted"
    )
    assert web.read_asset(src.group(1)) is not None, (
        f"the header points at {src.group(1)}, which is not shipped"
    )

    assert 'alt=""' in tag.group(0), (
        "the icon needs empty alt text: the word beside it already names the app, "
        "so anything else has a screen reader announce it twice"
    )


def test_the_header_logo_holds_its_shape():
    """It shares a flex row with the title and three controls.

    Without flex:none it is the one item in that row that will happily squash
    when a long header runs out of width, and a squashed portrait looks like a
    rendering fault rather than a smaller logo.
    """
    css = web.read_asset("app.css").decode()

    rule = re.search(r"\.hdr-logo \{(.*?)\}", css, re.S)
    assert rule, "the header logo is not styled"
    body = rule.group(1)
    assert "flex: none" in body

    width = re.search(r"width:\s*(\d+)px", body)
    height = re.search(r"height:\s*(\d+)px", body)
    assert width and height, "the logo has no explicit size"
    assert width.group(1) == height.group(1), (
        f"the logo is {width.group(1)}x{height.group(1)}, but the icon is square"
    )


def test_only_the_wordmark_is_dropped_on_a_very_narrow_phone():
    """The list header holds four controls now, and 320px cannot fit them all.

    The title is ``flex: 1``, so it is what absorbs a shortfall — it ellipsises
    and then collapses to nothing, which reads as a rendering fault. The word is
    dropped deliberately instead, since the icon beside it already says it.

    The scoping is the part worth guarding. ``.hdr-title`` also carries the task
    name in a conversation header and the page name everywhere else; hiding
    *that* on a narrow phone would leave a task's page with no title at all,
    and nothing else in the suite looks at a 320px viewport.
    """
    css = web.read_asset("app.css").decode()
    js = web.read_asset("app.js").decode()

    query = re.search(
        r"@media \(max-width:\s*(\d+)px\) \{(.*?)\n\}", css, re.S,
    )
    assert query, "nothing adapts the header to a narrow phone"
    assert ".hdr-wordmark" in query.group(2), (
        f"the narrow-phone rule targets something else: {query.group(2).strip()}"
    )
    assert not re.search(r"\.hdr-title\s*[,{]", query.group(2)), (
        "the rule hides every header title, not just the list's wordmark"
    )

    # Only the list's own title carries the class, so only it can be dropped.
    holders = re.findall(r'class="hdr-title([^"]*)"', js)
    assert holders, "no header titles found"
    assert sum("hdr-wordmark" in h for h in holders) == 1, (
        f"expected exactly one wordmark among the header titles, got {holders}"
    )
    assert re.search(r'class="hdr-title hdr-wordmark">ilan<', js), (
        "the wordmark class is not on the word it is meant to drop"
    )


def test_the_tap_action_is_still_the_warm_one():
    """Tap has been the yellow one across several rounds of this, so the hue is
    held even though the treatment that carried it is gone.

    Asserted as a property rather than against a literal, since the exact value
    differs per scheme and has moved more than once: warm means the red channel
    leads and the blue trails. Nothing else would catch a drift — a cool grey
    of the same lightness passes every contrast check in this file.
    """
    css = web.read_asset("app.css").decode()
    light, dark = _scheme_values(css)

    for scheme, values in (("light", light), ("dark", dark)):
        ink = values["--act-tap"]
        r, g, b = (int(ink[i:i + 2], 16) for i in (1, 3, 5))
        assert r > g > b, (
            f"{scheme}: --act-tap is {ink}, which is not a warm colour any more"
        )


def test_the_close_action_is_not_green():
    """Asked for explicitly, so it is worth being unable to drift back.

    Checked as a property of the colour rather than against the old literal:
    green is where the green channel leads, and any of the greens this used to
    be would trip it. The hue is constrained anyway — the status palette
    already spends cyan, coral, green, sage, grey and lavender — but "not
    green" is the part that was asked for.
    """
    css = web.read_asset("app.css").decode()
    light, dark = _scheme_values(css)

    for scheme, values in (("light", light), ("dark", dark)):
        ink = values["--act-done"]
        r, g, b = (int(ink[i:i + 2], 16) for i in (1, 3, 5))
        assert not (g > r and g > b), (
            f"{scheme}: --act-done is {ink}, whose green channel leads — "
            "it is a green again"
        )

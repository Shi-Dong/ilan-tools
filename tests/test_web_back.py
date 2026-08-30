"""The back control's size.

It is a single narrow glyph, so it is the one button whose *mark* can be too
small even though its tap target is fine. Nothing else would notice a
regression — a smaller glyph still renders, still works, and still passes
every behavioural test.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).parent.parent / "src" / "ilan" / "web" / "static"


def test_every_back_button_uses_the_enlarged_style() -> None:
    js = (STATIC / "app.js").read_text()
    backs = re.findall(r'<button class="([^"]*)" id="back"', js)
    assert backs, "no back button found"
    for classes in backs:
        assert "btn-back" in classes, f"a back button is missing btn-back: {classes}"
        # btn-sm is what made it small in the first place.
        assert "btn-sm" not in classes, f"a back button is still small: {classes}"


def test_the_back_glyph_is_large_and_fills_the_tap_target() -> None:
    css = (STATIC / "app.css").read_text()
    rule = re.search(r"\.btn-back \{(.*?)\}", css, re.S)
    assert rule, ".btn-back is not styled"
    body = rule.group(1)

    size = re.search(r"font-size:\s*(\d+)px", body)
    assert size and int(size.group(1)) >= 24, (
        f"back glyph is {size.group(1) if size else 'unset'}px; too small to aim at"
    )
    assert "min-height: var(--tap)" in body
    assert "width: var(--tap)" in body

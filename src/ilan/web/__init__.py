"""Static assets for the ilan web app.

The web app is a phone-first front end over the very same HTTP API the CLI
drives, so the server needs no new task logic to support it — only a way to
hand these files to a browser.

There is deliberately no build step and no JavaScript dependency: the files in
``static/`` ship verbatim inside the package and are served as-is. That keeps
``uv pip install`` the whole install story, and it means the app works on a
machine with no network access to a package registry.

All asset reads funnel through :func:`read_asset`, which refuses any path that
escapes ``static/`` so a crafted URL cannot turn the server into an arbitrary
file reader.
"""

from __future__ import annotations

from pathlib import Path

STATIC_DIR = Path(__file__).parent / "static"

INDEX_FILE = "index.html"

_CONTENT_TYPES: dict[str, str] = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    # Without this the icons fall through to application/octet-stream. Browsers
    # mostly sniff their way past that, but an apple-touch-icon served as a
    # generic byte stream is exactly the kind of thing iOS silently declines,
    # falling back to a screenshot of the page for the home-screen tile.
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webmanifest": "application/manifest+json",
}

DEFAULT_CONTENT_TYPE = "application/octet-stream"


def content_type(relative: str) -> str:
    """Return the Content-Type to serve ``static/<relative>`` with."""
    return _CONTENT_TYPES.get(Path(relative).suffix, DEFAULT_CONTENT_TYPE)


def read_asset(relative: str) -> bytes | None:
    """Return the bytes of ``static/<relative>``, or ``None`` if there is no
    such asset.

    ``None`` also covers every path that resolves outside ``static/`` — via
    ``..`` segments, an absolute path, or a symlink pointing elsewhere — so the
    caller can treat the result as a plain 404 without doing its own vetting.
    """
    root = STATIC_DIR.resolve()
    try:
        candidate = (STATIC_DIR / relative).resolve()
    except OSError:
        # A path long enough to fail resolution is not an asset either.
        return None
    if candidate != root and root not in candidate.parents:
        return None
    if not candidate.is_file():
        return None
    return candidate.read_bytes()

"""Helpers shared by the test modules that run a real :class:`IlanServer`."""

from __future__ import annotations

import time
from urllib.error import URLError
from urllib.request import urlopen

from ilan.server import IlanServer


def wait_until_serving(server: IlanServer, timeout: float = 5.0) -> int:
    """Return *server*'s port once it is actually answering requests.

    ``server._httpd`` appears as soon as the socket is bound, and ``run()``
    binds it *before* calling ``runner.recover()``. Handing control back to a
    test at that point lets the test seed its tasks while recovery is still in
    flight; recovery then reclaims those freshly seeded tasks, clearing the pid
    that a later kill assertion is waiting on, and the test fails for a reason
    that has nothing to do with what it is checking.

    A served ``/health`` response cannot happen before ``serve_forever()`` is
    running, which is strictly after ``recover()`` has returned — so waiting on
    one is the cheapest signal that actually orders the two.
    """
    deadline = time.monotonic() + timeout

    port: int | None = None
    while time.monotonic() < deadline:
        if server._httpd is not None:
            port = server._httpd.server_address[1]
            break
        time.sleep(0.05)
    assert port is not None, "Server did not start in time"

    while True:
        assert time.monotonic() < deadline, "Server did not begin serving in time"
        try:
            with urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5):
                return port
        except (URLError, OSError):
            time.sleep(0.02)

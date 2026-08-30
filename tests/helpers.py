"""Helpers shared by the test modules that run a real :class:`IlanServer`."""

from __future__ import annotations

import time
from urllib.error import URLError
from urllib.request import urlopen

from ilan.server import IlanServer

# How often a test server's serve loop wakes to check whether it should stop.
# ``shutdown()`` blocks for up to one tick, and the fixtures are function
# scoped, so this is paid once per test that starts a server — the default
# 0.5s would add minutes across the suite, and even 0.01s cost a few seconds.
# Every fixture in the suite passes this rather than its own copy.
SERVE_POLL_INTERVAL = 0.002


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

    Both waits poll on a 1ms tick. What is being waited for is another thread
    binding a socket and returning from ``recover()``, which takes a millisecond
    or two — so a coarser tick does not sleep until the server is ready, it
    sleeps past it. The first loop originally ticked at 50ms, and since the
    thread has only just been started it essentially always slept the full
    50ms before looking again. Function-scoped, that put a 50ms floor under
    every test in the suite that starts a server: about ten seconds in total,
    which was more than every web test put together.
    """
    deadline = time.monotonic() + timeout

    while server._httpd is None:
        assert time.monotonic() < deadline, "Server did not start in time"
        time.sleep(0.001)
    port = server._httpd.server_address[1]

    while True:
        assert time.monotonic() < deadline, "Server did not begin serving in time"
        try:
            with urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5):
                return port
        except (URLError, OSError):
            time.sleep(0.001)

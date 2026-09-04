"""
The connection pool must survive more requests than it has connections.

WHY THIS IS A TEST AND NOT A CODE REVIEW ITEM
---------------------------------------------
`ThreadingHTTPServer` starts a new thread per request, and both `student/db.py`
and `billing/mount.py` cache connections in `threading.local()`. That
combination means the cache is EMPTY on every request, so every request opens
a connection.

With SQLite that costs a file handle and nobody notices. With Postgres it is a
new backend per request against a bounded pool, and the failure mode is the
nasty kind: the service keeps answering until the pool is drained, then blocks,
then times out, while `/health` still reports the database as reachable
because the health check is holding one of the connections it already has.

So the test drives more sequential requests through a live server than the
pool has connections. Before the `finally: db.release()` in
`student/server.py`, this hangs at request nine of a pool of eight.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest


@pytest.fixture
def live_server(pg_schema, tmp_path):
    """A real socket, a real Postgres pool, torn down afterwards."""
    from persistence.postgres import Pool
    from student.server import build_api, make_handler

    api = build_api(tmp_path / "quintek.db", with_ai=False)
    handler = make_handler(api)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        Pool.close_all()


def _get(base: str, path: str, timeout: float = 10.0):
    with urllib.request.urlopen(f"{base}{path}", timeout=timeout) as response:
        return response.status, json.loads(response.read())


def test_more_requests_than_pool_connections_still_succeed(live_server):
    """
    Thirty requests through a pool of eight.

    Each one lands on a fresh thread with an empty thread-local cache, so each
    one checks a connection out. If they are not returned, this blocks well
    before the thirtieth.
    """
    from persistence.postgres import DEFAULT_MAX_POOL

    total = DEFAULT_MAX_POOL * 3 + 6
    assert total > DEFAULT_MAX_POOL, "the test has to exceed the pool to mean anything"

    for index in range(total):
        status, body = _get(live_server, "/health")
        assert status == 200, f"request {index + 1} of {total} returned {status}"
        assert body["database"] is True


def test_concurrent_requests_exceeding_the_pool_all_complete(live_server):
    """
    The same pressure, in parallel rather than in sequence.

    A pool that is merely slow to return connections passes the sequential
    test and fails this one.
    """
    from persistence.postgres import DEFAULT_MAX_POOL

    total = DEFAULT_MAX_POOL * 4
    results: list[int] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(total)

    def hit():
        try:
            barrier.wait(timeout=30)
            status, _ = _get(live_server, "/health", timeout=30.0)
            results.append(status)
        except BaseException as exc:          # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=hit) for _ in range(total)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=90)

    assert not errors, f"{len(errors)} of {total} requests failed: {errors[:3]!r}"
    assert results == [200] * total


def test_a_request_that_errors_still_returns_its_connection(live_server):
    """
    The `finally` has to cover the failure path too.

    A handler that returns its connection only on success leaks one per error,
    which is worse than leaking on every request: it works in testing and
    drains in production exactly when something else is already wrong.
    """
    from persistence.postgres import DEFAULT_MAX_POOL

    for _ in range(DEFAULT_MAX_POOL * 2):
        try:
            _get(live_server, "/no/such/route")
        except urllib.error.HTTPError as exc:
            # 401, because authentication is checked before routing -- an
            # unknown path must not reveal whether it exists. Any of these is
            # an error response, which is all this test needs.
            assert exc.code in (400, 401, 404, 405)

    status, body = _get(live_server, "/health")
    assert status == 200 and body["database"] is True


def test_writes_survive_a_connection_returning_to_the_pool(live_server, pg_schema):
    """
    Data written on one pooled connection is visible from the next.

    Guards against a return path that rolls back committed work -- the pool
    rolls back on check-in, deliberately, and that must not undo a commit.
    """
    from student.db import Database

    first = Database()
    uid = first.create_user("pooled@example.com", "password123")
    first.release()

    second = Database()
    row = second.query_one("SELECT email FROM users WHERE id = ?", (uid,))
    assert row is not None and row["email"] == "pooled@example.com"
    second.release()

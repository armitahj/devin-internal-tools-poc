import threading
from collections.abc import Callable

from fastapi.testclient import TestClient

from apps.main import app


def client_as(user_id: str) -> TestClient:
    client = TestClient(app, follow_redirects=False)
    client.cookies.set("user_id", user_id)
    return client


def run_concurrently(*calls: Callable[[], object]) -> list[object]:
    """Start all calls at the same moment on separate threads; return results in order."""
    barrier = threading.Barrier(len(calls))
    results: list[object] = [None] * len(calls)

    def run(i: int, call: Callable[[], object]) -> None:
        barrier.wait()
        try:
            results[i] = call()
        except Exception as exc:  # surfaced to the test
            results[i] = exc

    threads = [threading.Thread(target=run, args=(i, c)) for i, c in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results

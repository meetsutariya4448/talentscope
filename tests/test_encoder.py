"""
Regression test for a real race found under load testing (see
evals/load-test.md): get_model()'s lazy-init check (`if _model is None`)
had no lock, so concurrent requests on FastAPI's threadpool could both
construct SentenceTransformer(...) at once — which raised a real
request-serving 500 ("Cannot copy out of meta tensor; no data!") at just 2
concurrent vector-search requests against a cold process.
"""
import threading
from unittest.mock import patch

import app.search.encoder as encoder_mod


def test_get_model_constructs_exactly_once_under_concurrency():
    encoder_mod._model = None
    construct_count = 0
    construct_lock = threading.Lock()

    def fake_sentence_transformer(*args, **kwargs):
        nonlocal construct_count
        with construct_lock:
            construct_count += 1
        # Simulate real load-time cost so concurrent callers actually
        # overlap inside the critical section rather than serializing by
        # accident — this is what makes the un-locked version of
        # get_model() flaky rather than reliably failing.
        threading.Event().wait(0.05)
        return object()

    barrier = threading.Barrier(10)

    def call_get_model():
        barrier.wait()  # maximize overlap: all threads hit get_model() together
        encoder_mod.get_model()

    with patch(
        "sentence_transformers.SentenceTransformer",
        side_effect=fake_sentence_transformer,
    ):
        threads = [threading.Thread(target=call_get_model) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

    assert construct_count == 1, (
        f"SentenceTransformer constructed {construct_count} times under concurrent "
        "access — get_model()'s lock isn't preventing the double-construction race."
    )

    encoder_mod._model = None  # don't leak state into other tests

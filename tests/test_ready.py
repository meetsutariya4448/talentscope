"""Readiness must not advertise vector search before the model can serve it.

The bug these cover: /ready checked only the database and Redis, while the
embedding model was lazy-loaded per process with no warmup anywhere. A freshly
started process therefore reported itself Ready, and the first vector/hybrid
request paid the entire model load inside the request.
"""

import threading

from app.search import encoder


def test_health_stays_ok_while_model_is_cold(client):
    """Liveness must never depend on the model.

    This is the case that matters operationally: if /health went 503 during a
    slow load, the orchestrator would kill the container and restart it into
    exactly the same slow load, forever.
    """
    encoder._ready.clear()

    resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ready_reports_degraded_while_model_is_cold(client):
    encoder._ready.clear()

    resp = client.get("/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["embedding_model"] is False


def test_ready_lists_the_model_as_its_own_check(client):
    """DB and Redis readiness must not be able to mask a cold model."""
    encoder._ready.set()

    body = client.get("/ready").json()

    assert set(body["checks"]) == {"database", "redis", "embedding_model"}
    assert body["checks"]["embedding_model"] is True


def test_vector_search_refuses_rather_than_loading_in_request(client, monkeypatch):
    """A cold process must fail fast and retriably, not block on the load."""
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError(
            "vector_search ran on a cold process — the request was about to "
            "pay the model load inline, which is the whole thing being fixed"
        )

    monkeypatch.setattr("app.api.postings.vector_search", fail_if_called)
    encoder._ready.clear()

    resp = client.get("/postings/?mode=vector&q=python+engineer")

    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "5"


def test_hybrid_search_refuses_while_cold(client, monkeypatch):
    monkeypatch.setattr(
        "app.api.postings.vector_search",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("ran while cold")),
    )
    encoder._ready.clear()

    resp = client.get("/postings/?mode=hybrid&q=backend+engineer")

    assert resp.status_code == 503


def test_fts_search_is_unaffected_by_a_cold_model(client):
    """FTS never touches the encoder, so the gate must not apply to it."""
    encoder._ready.clear()

    resp = client.get("/postings/?mode=fts&q=python")

    assert resp.status_code == 200
    assert resp.json()["mode"] == "fts"


def test_blank_query_still_falls_back_without_tripping_the_gate(client):
    """The pre-existing empty-query fallback runs before the readiness gate,
    so a blank vector query is served by FTS even on a cold process rather
    than being rejected for a model it was never going to use."""
    encoder._ready.clear()

    resp = client.get("/postings/?mode=vector&q=%20%20%20")

    assert resp.status_code == 200
    assert resp.json()["mode"] == "fts"


def test_warm_model_sets_readiness_and_records_duration(monkeypatch):
    """warm_model() must run a real encode, not just construct the model.

    Constructing SentenceTransformer leaves per-submodule tensors unmaterialised;
    the first encode() is where a meaningful part of the cold-start cost lives.
    Flipping readiness on construction alone would still leave the first real
    search slow.
    """
    encoded = []

    class _FakeModel:
        def encode(self, texts, **_kwargs):
            encoded.append(texts)
            return [[0.0] * 384 for _ in texts]

    monkeypatch.setattr(encoder, "_model", _FakeModel())
    encoder._ready.clear()

    elapsed = encoder.warm_model()

    assert encoded, "warm_model() did not run an encode"
    assert encoder.is_model_ready() is True
    assert elapsed >= 0
    assert encoder.model_load_seconds() == elapsed


def test_model_construction_alone_does_not_mark_the_process_ready(monkeypatch):
    """Construction is only a small part of warmup; the first encode must finish."""
    monkeypatch.setattr(encoder, "_model", object())
    encoder._ready.clear()

    encoder.get_model()

    assert encoder.is_model_ready() is False


def test_warmup_does_not_mark_ready_until_first_encode_finishes(monkeypatch):
    encode_started = threading.Event()
    release_encode = threading.Event()

    class _BlockingModel:
        def encode(self, _texts, **_kwargs):
            encode_started.set()
            assert release_encode.wait(timeout=2)
            return [[0.0] * 384]

    monkeypatch.setattr(encoder, "_model", _BlockingModel())
    encoder._ready.clear()
    warmup = threading.Thread(target=encoder.warm_model)
    warmup.start()

    assert encode_started.wait(timeout=2)
    assert encoder.is_model_ready() is False

    release_encode.set()
    warmup.join(timeout=2)
    assert not warmup.is_alive()
    assert encoder.is_model_ready() is True

"""Unit tests for ogsim.market.anomalies.AnomalyStore: injection, targeting,
expiry and sweeping."""

from __future__ import annotations

from ogsim.market.anomalies import Anomaly, AnomalyStore


def make_anomaly(anomaly_id: str, target: str = "*", start: float = 0.0, duration: float = 60.0) -> Anomaly:
    return Anomaly(
        id=anomaly_id, type="price_spike", target=target, params={}, start=start, duration=duration
    )


def test_injected_anomaly_is_active_within_its_window():
    store = AnomalyStore()
    store.inject(make_anomaly("a1", start=100.0, duration=50.0))
    assert store.active(now=120.0)
    assert not store.active(now=99.0)
    assert not store.active(now=151.0)


def test_active_filters_by_target_product():
    store = AnomalyStore()
    store.inject(make_anomaly("a1", target="np6-905-cd", start=0.0, duration=100.0))
    assert store.active("np6-905-cd", now=10.0)
    assert not store.active("np4-188-cd", now=10.0)


def test_wildcard_target_applies_to_every_product():
    store = AnomalyStore()
    store.inject(make_anomaly("a1", target="*", start=0.0, duration=100.0))
    assert store.active("np6-905-cd", now=10.0)
    assert store.active("anything", now=10.0)


def test_cancel_removes_an_anomaly_immediately():
    store = AnomalyStore()
    store.inject(make_anomaly("a1", start=0.0, duration=1000.0))
    assert store.cancel("a1") is True
    assert not store.active(now=10.0)


def test_cancel_unknown_id_returns_false():
    store = AnomalyStore()
    assert store.cancel("does-not-exist") is False


def test_sweep_expired_drops_anomalies_past_the_grace_period():
    store = AnomalyStore()
    store.inject(make_anomaly("old", start=0.0, duration=10.0))
    store.sweep_expired(now=10.0 + 3600.0 + 1.0)
    assert store.all() == []


def test_sweep_expired_keeps_recently_expired_anomalies():
    store = AnomalyStore()
    store.inject(make_anomaly("recent", start=0.0, duration=10.0))
    store.sweep_expired(now=20.0)
    assert len(store.all()) == 1

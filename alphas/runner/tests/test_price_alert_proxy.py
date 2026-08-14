"""Tests for the runner-side PriceAlertProxy registration + channel wiring."""

from __future__ import annotations

import json

from runner.strategy.context import PriceAlertProxy


class _FakeMds:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    def publish(self, channel: str, payload: str) -> None:
        self.published.append((channel, payload))


def test_sync_publishes_registration_to_mds_subscribe_channel():
    mds = _FakeMds()
    proxy = PriceAlertProxy(
        symbols=set(),
        alpha_id="xau-m30-alpha-10",
        exchange="binance",
        mds_client=mds,
    )

    proxy.sync({"PAXGUSDT"})

    assert len(mds.published) == 1
    channel, raw = mds.published[0]
    assert channel == "price_alert:subscribe:binance"
    payload = json.loads(raw)
    assert payload["consumer_id"] == "xau-m30-alpha-10"
    assert payload["action"] == "sync"
    assert payload["symbols"] == ["PAXGUSDT"]


def test_sync_clears_registration_when_no_positions():
    mds = _FakeMds()
    proxy = PriceAlertProxy(
        symbols={"PAXGUSDT"},
        alpha_id="xau-m30-alpha-10",
        exchange="binance",
        mds_client=mds,
    )

    proxy.sync(set())

    channel, raw = mds.published[-1]
    assert channel == "price_alert:subscribe:binance"
    assert json.loads(raw)["symbols"] == []


def test_active_prefixed_channels_maps_symbols_to_mds_channels():
    proxy = PriceAlertProxy(symbols={"PAXGUSDT", "XAUUSDT"}, exchange="binance")
    assert proxy.active_prefixed_channels() == {
        "price_alert:binance:PAXGUSDT",
        "price_alert:binance:XAUUSDT",
    }


def test_no_registration_without_mds_client():
    proxy = PriceAlertProxy(symbols=set(), alpha_id="a", exchange="binance")
    proxy.sync({"PAXGUSDT"})
    assert proxy.symbols == {"PAXGUSDT"}


def test_no_channels_without_exchange():
    proxy = PriceAlertProxy(symbols={"PAXGUSDT"})
    assert proxy.active_prefixed_channels() == set()

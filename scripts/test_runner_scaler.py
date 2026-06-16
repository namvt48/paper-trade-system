from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def load_scaler():
    path = Path(__file__).resolve().parent / "runner-scaler.py"
    spec = importlib.util.spec_from_file_location("runner_scaler", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_scale_up_after_sustained_high_load():
    scaler = load_scaler()
    state = scaler.ScaleState()
    loads = [scaler.RunnerLoad(cpu_pct=80, memory_pct=40)]

    assert scaler.decide_scale(loads, 1, state, now=0, sustain_sec=10, cooldown_sec=0) == 1
    assert scaler.decide_scale(loads, 1, state, now=11, sustain_sec=10, cooldown_sec=0) == 2


def test_no_scale_up_during_cooldown():
    scaler = load_scaler()
    state = scaler.ScaleState(high_since=0, last_action_at=95)
    loads = [scaler.RunnerLoad(cpu_pct=90, memory_pct=40)]

    assert scaler.decide_scale(loads, 2, state, now=100, sustain_sec=10, cooldown_sec=30) == 2


def test_no_scale_down_below_minimum():
    scaler = load_scaler()
    state = scaler.ScaleState()
    loads = [scaler.RunnerLoad(cpu_pct=10, memory_pct=10)]

    assert scaler.decide_scale(loads, 1, state, now=0, sustain_sec=1, cooldown_sec=0) == 1
    assert scaler.decide_scale(loads, 1, state, now=2, sustain_sec=1, cooldown_sec=0) == 1


def test_scaler_lock_prevents_two_scalers_acting():
    scaler = load_scaler()

    class FakeRedis:
        def __init__(self):
            self.locked = False

        def set(self, key, value, nx=False, ex=None):
            if nx and self.locked:
                return False
            self.locked = True
            return True

    redis = FakeRedis()
    assert scaler.acquire_lock(redis, "one") is True
    assert scaler.acquire_lock(redis, "two") is False

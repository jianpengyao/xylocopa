"""Login limiter + global backstop: lock only the failing client, briefly.

Never-lockout guarantees covered here: G3 (the longest lock is 15 min), and
the building blocks of G1/G2 (per-key state, loopback detection). The route
level checks (another IP still logs in, loopback never 429) live in
tests/test_auth_routes.py.
"""

import asyncio

import pytest

import auth
import config
from auth import GlobalFailureMeter, LoginRateLimiter, is_loopback, normalize_ip

T0 = 1_800_000_000.0
IP = "100.64.0.10"


def _fail(rl, n, now, key=IP, **kw):
    out = None
    for _ in range(n):
        out = rl.record_failure(key, now=now, **kw)
    return out


def test_defaults_match_contract():
    rl = LoginRateLimiter()
    assert (rl.threshold, rl.base_seconds, rl.max_seconds) == (5, 60, 900)
    assert (rl.forget_seconds, rl.max_entries) == (900, 4096)
    assert auth.login_limiter is not auth.change_password_limiter
    assert config.AUTH_LIMIT_MAX_S <= 15 * 60  # G3


def test_lock_at_threshold_then_unlock_with_failures_decayed():
    rl = LoginRateLimiter()
    for i in range(4):
        assert rl.record_failure(IP, now=T0 + i) == (False, 0)
        assert rl.check(IP, now=T0 + i) == (False, 0)
    assert rl.record_failure(IP, now=T0 + 4) == (True, 60)
    assert rl.check(IP, now=T0 + 4) == (True, 61)
    assert rl.check(IP, now=T0 + 63.5) == (True, 1)
    # Lock served: not locked, and the next single failure does not relock.
    assert rl.check(IP, now=T0 + 64) == (False, 0)
    assert rl.record_failure(IP, now=T0 + 65) == (False, 0)
    assert rl.check(IP, now=T0 + 65) == (False, 0)
    # Another full run of failures locks again, for twice as long.
    assert _fail(rl, 4, T0 + 66) == (True, 120)
    # Other keys are untouched throughout.
    assert rl.check("100.64.0.11", now=T0 + 66) == (False, 0)


def test_lock_doubles_and_caps_at_15_minutes():
    rl = LoginRateLimiter()
    now, durations = T0, []
    for _ in range(7):
        locked, secs = _fail(rl, 5, now)
        assert locked
        durations.append(secs)
        now += secs  # wait the lock out, then fail again within the forget window
    assert durations == [60, 120, 240, 480, 900, 900, 900]


def test_forgotten_after_15_quiet_minutes():
    rl = LoginRateLimiter()
    _fail(rl, 5, T0)  # lock 60 s
    _fail(rl, 5, T0 + 60)  # second lock: 120 s
    assert len(rl) == 1
    # Quiet for > forget_seconds after the last failure (and unlocked):
    # the entry, and with it the lock history, is dropped.
    assert rl.check(IP, now=T0 + 60 + 901) == (False, 0)
    assert len(rl) == 0
    assert _fail(rl, 5, T0 + 2000) == (True, 60)


def test_not_forgotten_while_locked():
    rl = LoginRateLimiter(threshold=1, base_seconds=2000, max_seconds=2000)
    rl.record_failure(IP, now=T0)
    assert rl.check(IP, now=T0 + 1500) == (True, 501)


def test_reset_at_drops_older_entries():
    rl = LoginRateLimiter()
    _fail(rl, 5, T0)
    assert rl.check(IP, now=T0 + 1)[0]
    # ./run.sh reset-password stamps auth_limiter_reset_at; the route passes it.
    assert rl.check(IP, now=T0 + 2, reset_at=T0 + 1) == (False, 0)
    assert len(rl) == 0
    # Failures after the reset count normally.
    assert rl.record_failure(IP, now=T0 + 3, reset_at=T0 + 1) == (False, 0)
    assert rl.check(IP, now=T0 + 3, reset_at=T0 + 1) == (False, 0)
    assert len(rl) == 1


def test_record_success_clears():
    rl = LoginRateLimiter()
    _fail(rl, 4, T0)
    rl.record_success(IP)
    assert _fail(rl, 4, T0 + 1) == (False, 0)


def test_bounded_entries_evict_oldest_unlocked_first():
    rl = LoginRateLimiter(threshold=2, max_entries=3)
    _fail(rl, 2, T0, key="locked-a")       # locked
    rl.record_failure("b", now=T0 + 1)     # unlocked, oldest unlocked
    rl.record_failure("c", now=T0 + 2)
    rl.record_failure("d", now=T0 + 3)     # 4th key → evict "b"
    assert len(rl) == 3
    assert set(rl._state) == {"locked-a", "c", "d"}
    assert rl.check("locked-a", now=T0 + 4)[0]

    rl = LoginRateLimiter(threshold=1, max_entries=2)
    for i, key in enumerate(("x", "y", "z")):
        rl.record_failure(key, now=T0 + i)  # every entry locked
    assert set(rl._state) == {"y", "z"}  # all locked → the oldest goes


def test_bounded_at_default_max_entries():
    rl = LoginRateLimiter()
    for i in range(5000):
        rl.record_failure(f"10.{i // 65536}.{(i // 256) % 256}.{i % 256}", now=T0 + i * 0.001)
    assert len(rl) == 4096


def test_one_at_a_time_serializes_a_key_and_frees_its_slot():
    rl = LoginRateLimiter()
    order = []

    async def attempt(key, tag):
        async with rl.one_at_a_time(key):
            order.append(f"{tag}+")
            await asyncio.sleep(0.01)
            order.append(f"{tag}-")

    async def main():
        await asyncio.gather(attempt(IP, "a"), attempt(IP, "b"), attempt(None, "x"), attempt(None, "y"))

    asyncio.run(main())
    # Same key: never overlapping. None (loopback): no limit.
    assert order.index("a-") < order.index("b+")
    assert order.index("y+") < order.index("x-")

    # The per-key slot exists only while in use.
    async def leftover():
        async with rl.one_at_a_time(IP):
            assert IP in rl._attempts()
        return dict(rl._attempts())

    assert asyncio.run(leftover()) == {}


def test_global_meter_delay_curve():
    m = GlobalFailureMeter()
    assert (m.window_seconds, m.soft_limit) == (300, 30)
    for i in range(30):
        m.record(now=T0 + i)
    assert m.delay(now=T0 + 30) == 0.0
    m.record(now=T0 + 30)
    assert m.delay(now=T0 + 31) == pytest.approx(0.1)
    for i in range(9):
        m.record(now=T0 + 31 + i)
    assert m.count(now=T0 + 40) == 40
    assert m.delay(now=T0 + 40) == pytest.approx(1.0)
    for i in range(100):
        m.record(now=T0 + 40 + i * 0.01)
    assert m.delay(now=T0 + 41) == 2.0  # capped, never a refusal
    # Failures leave the window after 300 s.
    assert m.delay(now=T0 + 41 + 301) == 0.0
    assert m.count(now=T0 + 41 + 301) == 0


def test_global_meter_throttle_sleeps(monkeypatch):
    m = GlobalFailureMeter()
    slept = []

    async def fake_sleep(d):
        slept.append(d)

    monkeypatch.setattr(auth.asyncio, "sleep", fake_sleep)
    assert asyncio.run(m.throttle()) == 0.0
    for _ in range(35):
        m.record()
    assert asyncio.run(m.throttle()) == pytest.approx(0.5)
    assert slept == [pytest.approx(0.5)]


@pytest.mark.parametrize("raw,norm,loop", [
    ("127.0.0.1", "127.0.0.1", True),
    ("127.5.6.7", "127.5.6.7", True),
    ("::1", "::1", True),
    ("[::1]", "::1", True),
    ("::ffff:127.0.0.1", "127.0.0.1", True),
    ("::ffff:100.64.0.10", "100.64.0.10", False),
    ("100.64.0.10", "100.64.0.10", False),
    ("192.0.2.20", "192.0.2.20", False),
    ("fe80::1", "fe80::1", False),
    ("testclient", "testclient", False),
    ("localhost", "localhost", False),
    (None, "unknown", False),
    ("", "unknown", False),
])
def test_normalize_ip_and_loopback(raw, norm, loop):
    assert normalize_ip(raw) == norm
    assert is_loopback(raw) is loop

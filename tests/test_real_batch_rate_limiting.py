"""The rate-limit handling that the first real batch run needed and did not have.

`tools/run_real_batch.py` fired ten invoice creates back to back against the
live account. Five succeeded; five came back `BadRequestError: Too many
requests`. No test caught it, and no test *could* have under the existing
setup -- `SimulatedRail` has no rate limit, so there is no such failure to
simulate. The bug lived in the one place the test doubles do not model.

These tests fix that by injecting the failure directly rather than by
teaching SimulatedRail to rate-limit. Modelling a quota in the simulator
would be inventing a number I have not measured (docs/SIMULATOR_PROVENANCE.md
is strict about that); injecting the exception the real rail actually
returned is not an invention -- the string below is copied from the live
traceback.
"""

from __future__ import annotations

import pytest

from tools import run_real_batch


class FakeRateLimited(Exception):
    """Stands in for razorpay.errors.BadRequestError, whose constructor is
    not part of any contract I want a test to depend on."""


class TestRateLimitDetection:
    def test_it_recognises_the_message_the_live_rail_actually_returned(self):
        assert run_real_batch._is_rate_limit(FakeRateLimited("Too many requests"))

    def test_it_is_case_insensitive(self):
        assert run_real_batch._is_rate_limit(FakeRateLimited("TOO MANY REQUESTS"))
        assert run_real_batch._is_rate_limit(FakeRateLimited("too many requests"))

    @pytest.mark.parametrize("message", [
        "The requested URL was not found",
        "Authentication failed",
        "amount must be at least 100",
        "You have exceeded the maximum number of payment links",
    ])
    def test_other_failures_are_not_treated_as_rate_limits(self, message):
        """The payment-link cap in particular must NOT retry: it is a
        permanent lifetime quota, and three attempts would produce the same
        answer three times while making the batch look flaky rather than
        blocked."""
        assert not run_real_batch._is_rate_limit(FakeRateLimited(message))


@pytest.fixture
def batch_env(tmp_path, monkeypatch):
    """Point the batch's ledgers at a tmp dir and remove the real sleeps, so
    these run in milliseconds and never touch docs/evidence."""
    monkeypatch.setattr(run_real_batch, "LEDGER_PATH", tmp_path / "ledger.db")
    monkeypatch.setattr(run_real_batch, "OUTBOUND_PATH", tmp_path / "outbound.db")
    monkeypatch.setattr(run_real_batch.time, "sleep", lambda _seconds: None)
    return tmp_path


def _rows(n=2):
    return run_real_batch.build_batch(n, seed=1, run_tag="test")


class TestRetryBehaviour:
    def test_a_rate_limited_row_is_retried_and_can_succeed(self, batch_env, monkeypatch):
        """The actual fix: a transient 'too many requests' must not cost a
        row, because on the live run it cost five."""
        calls = {"n": 0}
        sentinel = object()

        def flaky(**_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise FakeRateLimited("Too many requests")
            return _fake_result()

        monkeypatch.setattr(run_real_batch, "run_pipeline", flaky)
        results, chain_ok = run_real_batch.run_batch(_rows(1), rail=sentinel)

        assert calls["n"] == 2, "the row should have been attempted twice"
        assert "error" not in results[0]
        assert results[0]["external_ref"] == "inv_FAKE"
        assert chain_ok

    def test_it_gives_up_after_the_documented_number_of_attempts(self, batch_env, monkeypatch):
        calls = {"n": 0}

        def always_limited(**_kwargs):
            calls["n"] += 1
            raise FakeRateLimited("Too many requests")

        monkeypatch.setattr(run_real_batch, "run_pipeline", always_limited)
        results, _ = run_real_batch.run_batch(_rows(1), rail=object())

        assert calls["n"] == run_real_batch.MAX_RETRIES
        assert "Too many requests" in results[0]["error"]

    def test_a_non_rate_limit_error_is_not_retried(self, batch_env, monkeypatch):
        """Retrying a permanent failure wastes the batch's time and hides
        the real cause behind a flaky-looking log."""
        calls = {"n": 0}

        def permanent(**_kwargs):
            calls["n"] += 1
            raise FakeRateLimited("amount must be at least 100")

        monkeypatch.setattr(run_real_batch, "run_pipeline", permanent)
        results, _ = run_real_batch.run_batch(_rows(1), rail=object())

        assert calls["n"] == 1, "a permanent error must be attempted exactly once"
        assert "amount must be at least 100" in results[0]["error"]

    def test_one_failed_row_does_not_strand_the_rest(self, batch_env, monkeypatch):
        """This part already worked on the live run and is pinned so it keeps
        working: five failures did not prevent the other five from being
        recorded, and the hash chain still verified."""
        calls = {"n": 0}

        def first_row_fails(**_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise FakeRateLimited("Authentication failed")
            return _fake_result()

        monkeypatch.setattr(run_real_batch, "run_pipeline", first_row_fails)
        results, chain_ok = run_real_batch.run_batch(_rows(3), rail=object())

        assert len(results) == 3
        assert "error" in results[0]
        assert all("error" not in r for r in results[1:])
        assert chain_ok, "the ledger chain must verify even across a failed row"

    def test_rows_are_spaced_so_the_limit_is_not_hit_in_the_first_place(self, batch_env, monkeypatch):
        """Retrying is the safety net; spacing is the actual fix. Without
        this, a batch of 50 would spend most of its life in backoff."""
        sleeps: list[float] = []
        monkeypatch.setattr(run_real_batch.time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(run_real_batch, "run_pipeline", lambda **_k: _fake_result())

        run_real_batch.run_batch(_rows(4), rail=object())

        assert sleeps == [run_real_batch.SPACING_SECONDS] * 3, (
            "expected a pause between rows but not before the first one")


def _fake_result():
    """Minimal stand-in for OrchestrationResult -- only the attributes
    run_batch actually reads."""
    class _Outcome:
        external_ref = "inv_FAKE"
        detail = {"short_url": "https://rzp.io/i/fake", "status": "issued"}

    class _Result:
        action_type = type("_A", (), {"value": "reissue_artifact"})()
        ev_paise = 1000
        bounds_passed = True
        refusal_reasons: list[str] = []
        action_outcome = _Outcome()

    return _Result()

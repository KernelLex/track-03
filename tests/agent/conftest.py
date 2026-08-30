"""Shared test fixtures for agent/ tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


class FakeClock:
    """A controllable clock for tests that need to assert on elapsed time
    (e.g. the NOTIFIED_24H gate) without actually sleeping."""

    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))

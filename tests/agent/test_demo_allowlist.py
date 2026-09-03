"""Letting a second person try the demo, without opening the bot to anyone.

The inbound Telegram guard allowed exactly one hard-coded chat, so only the
demo owner could ever use the two-way flow -- useless when several judges
want to try it. This relaxes it by consent: a chat id becomes allowed when
someone deliberately enters it in the dashboard and asks for a send.

The property that must not be lost is fail-closed. `docs/WHAT_BROKE.md` #25
is the record of the last time this guard failed open -- `if configured and
chat_id != configured` skipped the check entirely when the variable was
unset, so an unconfigured deployment answered anyone.
"""

from __future__ import annotations

import time

import pytest

from agent.api.demo_allowlist import TelegramAllowlist


@pytest.fixture
def allowlist(tmp_path):
    with TelegramAllowlist(str(tmp_path / "allow.db")) as a:
        yield a


class TestConsent:
    def test_an_unknown_chat_is_not_allowed(self, allowlist):
        assert not allowlist.is_allowed("123456")

    def test_an_added_chat_is_allowed(self, allowlist):
        allowlist.allow("123456")
        assert allowlist.is_allowed("123456")

    def test_adding_one_chat_does_not_allow_another(self, allowlist):
        """The bug that would make this whole mechanism pointless."""
        allowlist.allow("123456")
        assert not allowlist.is_allowed("999999")

    def test_it_accepts_an_int_or_a_str(self, allowlist):
        """Telegram sends the chat id as a number in JSON and the webhook
        stringifies it; the dashboard sends a string. Both must match."""
        allowlist.allow(123456)
        assert allowlist.is_allowed("123456")

    def test_adding_twice_is_harmless(self, allowlist):
        allowlist.allow("123456")
        allowlist.allow("123456")
        assert allowlist.active() == ["123456"]

    def test_revoking_works(self, allowlist):
        allowlist.allow("123456")
        allowlist.revoke("123456")
        assert not allowlist.is_allowed("123456")

    def test_clear_empties_it(self, allowlist):
        allowlist.allow("1")
        allowlist.allow("2")
        assert allowlist.clear() == 2
        assert allowlist.active() == []


class TestExpiry:
    def test_an_old_entry_stops_being_allowed(self, tmp_path):
        """A demo audience is transient. An allowlist that only grows is one
        nobody can reason about a week later."""
        with TelegramAllowlist(str(tmp_path / "a.db"), ttl_seconds=0) as a:
            a.allow("123456")
            time.sleep(0.01)
            assert not a.is_allowed("123456")

    def test_an_expired_entry_is_removed_not_just_hidden(self, tmp_path):
        """A row that lingers past its TTL is a row that could later be
        misread as permission."""
        with TelegramAllowlist(str(tmp_path / "a.db"), ttl_seconds=0) as a:
            a.allow("123456")
            time.sleep(0.01)
            a.is_allowed("123456")
            assert a.active() == []

    def test_re_adding_refreshes_the_clock(self, tmp_path):
        """Someone still actively demoing must not be timed out mid
        conversation."""
        with TelegramAllowlist(str(tmp_path / "a.db"), ttl_seconds=3600) as a:
            a.allow("123456")
            first = a._conn.execute(
                "SELECT added_at FROM allowed_chats WHERE chat_id='123456'").fetchone()[0]
            time.sleep(0.01)
            a.allow("123456")
            second = a._conn.execute(
                "SELECT added_at FROM allowed_chats WHERE chat_id='123456'").fetchone()[0]
        assert second > first

    def test_active_excludes_expired_entries(self, tmp_path):
        with TelegramAllowlist(str(tmp_path / "a.db"), ttl_seconds=0) as a:
            a.allow("123456")
            time.sleep(0.01)
            assert a.active() == []


class TestPersistence:
    def test_it_survives_a_reopen(self, tmp_path):
        """The webhook opens a fresh handle per request, so an in-memory set
        would allow a chat for exactly one message."""
        path = str(tmp_path / "a.db")
        with TelegramAllowlist(path) as a:
            a.allow("123456")
        with TelegramAllowlist(path) as b:
            assert b.is_allowed("123456")

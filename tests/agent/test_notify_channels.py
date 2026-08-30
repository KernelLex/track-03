"""Tests for agent.notify.* — no real network calls. Every real channel is
exercised through httpx.MockTransport, asserting both the outbound request
shape (so a live credential swap has the best chance of working first try)
and the parsed MessageSendResult.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from agent.notify.protocol import ChannelUnavailable, MessageSendResult
from agent.notify.simulated import SimulatedChannel
from agent.notify.telegram import TelegramChannel
from agent.notify.twilio_voice import TwilioVoiceChannel


def _client_with(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class TestSimulatedChannel:
    def test_records_and_reports_sent(self):
        channel = SimulatedChannel()
        result = channel.send(to="debtor-1", text="hello")
        assert isinstance(result, MessageSendResult)
        assert result.status == "sent"
        assert result.channel == "simulated"
        assert channel.sent == [{"to": "debtor-1", "text": "hello"}]

    def test_external_ref_increments_and_stays_unique(self):
        channel = SimulatedChannel()
        first = channel.send(to="a", text="1")
        second = channel.send(to="a", text="2")
        assert first.external_ref != second.external_ref


class TestTelegramChannel:
    def test_send_success_posts_chat_id_and_text(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={"ok": True, "result": {"message_id": 42, "chat": {"id": 999}, "date": 1234}},
            )

        channel = TelegramChannel("test-token", client=_client_with(handler))
        result = channel.send(to="999", text="your invoice is ready")

        assert captured["url"].endswith("/bottest-token/sendMessage")
        assert captured["body"] == {"chat_id": "999", "text": "your invoice is ready"}
        assert result.status == "sent"
        assert result.external_ref == "42"
        assert result.detail["chat_id"] == 999

    def test_send_api_rejection_is_a_clean_failed_result_not_an_exception(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"ok": False, "description": "chat not found"})

        channel = TelegramChannel("test-token", client=_client_with(handler))
        result = channel.send(to="does-not-exist", text="hi")

        assert result.status == "failed"
        assert result.external_ref is None
        assert result.detail["description"] == "chat not found"

    def test_network_error_raises_channel_unavailable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        channel = TelegramChannel("test-token", client=_client_with(handler))
        with pytest.raises(ChannelUnavailable):
            channel.send(to="999", text="hi")

    def test_empty_bot_token_rejected_at_construction(self):
        with pytest.raises(ValueError):
            TelegramChannel("")

    def test_get_updates_returns_result_list(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True, "result": [{"message": {"chat": {"id": 555}}}]})

        channel = TelegramChannel("test-token", client=_client_with(handler))
        updates = channel.get_updates()
        assert updates[0]["message"]["chat"]["id"] == 555


class TestTwilioVoiceChannel:
    def test_send_success_posts_to_from_and_twiml(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["form"] = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            return httpx.Response(201, json={"sid": "CA123", "status": "queued"})

        channel = TwilioVoiceChannel("ACxxx", "authtoken", "+15551234567", client=_client_with(handler))
        result = channel.send(to="+919876543210", text="This is a call about your invoice")

        assert captured["url"].endswith("/Accounts/ACxxx/Calls.json")
        assert captured["form"]["To"] == "+919876543210"
        assert captured["form"]["From"] == "+15551234567"
        assert "<Say>This is a call about your invoice</Say>" in captured["form"]["Twiml"]
        assert result.status == "sent"
        assert result.external_ref == "CA123"

    def test_special_characters_are_xml_escaped_in_twiml(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["form"] = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            return httpx.Response(201, json={"sid": "CA1", "status": "queued"})

        channel = TwilioVoiceChannel("ACxxx", "authtoken", "+15551234567", client=_client_with(handler))
        channel.send(to="+919876543210", text="Amount due: A & B <Corp>")

        assert "A &amp; B &lt;Corp&gt;" in captured["form"]["Twiml"]
        assert "<Corp>" not in captured["form"]["Twiml"]

    def test_api_rejection_is_a_clean_failed_result(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"message": "The 'To' number is not a valid phone number."})

        channel = TwilioVoiceChannel("ACxxx", "authtoken", "+15551234567", client=_client_with(handler))
        result = channel.send(to="not-a-number", text="hi")

        assert result.status == "failed"
        assert result.external_ref is None

    def test_network_error_raises_channel_unavailable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        channel = TwilioVoiceChannel("ACxxx", "authtoken", "+15551234567", client=_client_with(handler))
        with pytest.raises(ChannelUnavailable):
            channel.send(to="+919876543210", text="hi")

    def test_missing_credentials_rejected_at_construction(self):
        with pytest.raises(ValueError):
            TwilioVoiceChannel("", "authtoken", "+15551234567")
        with pytest.raises(ValueError):
            TwilioVoiceChannel("ACxxx", "", "+15551234567")
        with pytest.raises(ValueError):
            TwilioVoiceChannel("ACxxx", "authtoken", "")

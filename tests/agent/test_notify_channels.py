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

    def test_get_updates_sends_offset_when_given(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json={"ok": True, "result": []})

        channel = TelegramChannel("test-token", client=_client_with(handler))
        channel.get_updates(offset=42)
        assert captured["params"]["offset"] == "42"

    def test_get_updates_omits_offset_when_not_given(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json={"ok": True, "result": []})

        channel = TelegramChannel("test-token", client=_client_with(handler))
        channel.get_updates()
        assert "offset" not in captured["params"]

    def test_get_me_returns_bot_identity(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True, "result": {"id": 123, "username": "truecommit_bot"}})

        channel = TelegramChannel("test-token", client=_client_with(handler))
        me = channel.get_me()
        assert me["username"] == "truecommit_bot"

    def test_get_me_raises_on_invalid_token(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

        channel = TelegramChannel("bad-token", client=_client_with(handler))
        with pytest.raises(ChannelUnavailable):
            channel.get_me()


class TestTwilioVoiceChannel:
    def test_can_authenticate_via_an_api_key_instead_of_the_classic_auth_token(self):
        """No classic Auth Token -- an API Key SID/Secret pair authenticates
        instead, with the API Key SID as the basic-auth username and the
        account_sid still used in the URL path."""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["authorization"] = request.headers["authorization"]
            captured["url"] = str(request.url)
            return httpx.Response(201, json={"sid": "CA1", "status": "queued"})

        channel = TwilioVoiceChannel(
            "ACxxx", "the-api-key-secret", "+15551234567",
            auth_username="SKxxx", transport=httpx.MockTransport(handler),
        )
        channel.send(to="+919876543210", text="hi")

        import base64
        expected = "Basic " + base64.b64encode(b"SKxxx:the-api-key-secret").decode()
        assert captured["authorization"] == expected
        assert captured["url"].endswith("/Accounts/ACxxx/Calls.json")  # account_sid, not the API key, in the path


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

    def test_verify_credentials_fetches_the_account_resource_not_a_call(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"friendly_name": "My test account", "status": "active"})

        channel = TwilioVoiceChannel("ACxxx", "authtoken", "+15551234567", client=_client_with(handler))
        info = channel.verify_credentials()

        assert captured["method"] == "GET"
        assert captured["url"].endswith("/Accounts/ACxxx.json")
        assert info == {"friendly_name": "My test account", "status": "active"}

    def test_verify_credentials_returns_none_on_bad_auth(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "Authenticate"})

        channel = TwilioVoiceChannel("ACxxx", "wrong", "+15551234567", client=_client_with(handler))
        assert channel.verify_credentials() is None

    def test_verify_credentials_returns_none_on_network_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        channel = TwilioVoiceChannel("ACxxx", "authtoken", "+15551234567", client=_client_with(handler))
        assert channel.verify_credentials() is None

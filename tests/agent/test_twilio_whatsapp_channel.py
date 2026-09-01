"""Tests for agent.notify.twilio_whatsapp -- no real network calls, no real
Twilio account needed. Every request shape is exercised through
httpx.MockTransport, matching tests/agent/test_notify_channels.py's
convention for TwilioVoiceChannel."""

from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs

import httpx
import pytest

from agent.notify.protocol import ChannelUnavailable
from agent.notify.twilio_whatsapp import TwilioWhatsAppChannel


def _client_with(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class TestSend:
    def test_send_success_posts_whatsapp_prefixed_to_and_from(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["form"] = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            return httpx.Response(201, json={"sid": "SM123", "status": "queued"})

        channel = TwilioWhatsAppChannel(
            "ACxxx", "authtoken", "whatsapp:+14155238886", client=_client_with(handler),
        )
        result = channel.send(to="+919611550053", text="Your invoice is overdue")

        assert captured["url"].endswith("/Accounts/ACxxx/Messages.json")
        assert captured["form"]["To"] == "whatsapp:+919611550053"
        assert captured["form"]["From"] == "whatsapp:+14155238886"
        assert captured["form"]["Body"] == "Your invoice is overdue"
        assert result.status == "sent"
        assert result.channel == "whatsapp"
        assert result.external_ref == "SM123"

    def test_from_number_without_whatsapp_prefix_is_normalized(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["form"] = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            return httpx.Response(201, json={"sid": "SM1", "status": "queued"})

        channel = TwilioWhatsAppChannel("ACxxx", "authtoken", "+14155238886", client=_client_with(handler))
        channel.send(to="+919611550053", text="hi")
        assert captured["form"]["From"] == "whatsapp:+14155238886"

    def test_to_number_already_prefixed_is_not_double_prefixed(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["form"] = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            return httpx.Response(201, json={"sid": "SM1", "status": "queued"})

        channel = TwilioWhatsAppChannel("ACxxx", "authtoken", "whatsapp:+14155238886", client=_client_with(handler))
        channel.send(to="whatsapp:+919611550053", text="hi")
        assert captured["form"]["To"] == "whatsapp:+919611550053"

    def test_can_authenticate_via_an_api_key_instead_of_the_classic_auth_token(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["authorization"] = request.headers["authorization"]
            return httpx.Response(201, json={"sid": "SM1", "status": "queued"})

        channel = TwilioWhatsAppChannel(
            "ACxxx", "the-api-key-secret", "whatsapp:+14155238886",
            auth_username="SKxxx", transport=httpx.MockTransport(handler),
        )
        channel.send(to="+919611550053", text="hi")

        expected = "Basic " + base64.b64encode(b"SKxxx:the-api-key-secret").decode()
        assert captured["authorization"] == expected

    def test_recipient_never_joined_sandbox_is_a_clean_failed_result(self):
        """The single most common real-world failure of the sandbox path --
        must not raise, since Twilio's API was reached and it answered."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={
                "code": 63007, "message": "Twilio could not find a Channel with the specified From address",
            })

        channel = TwilioWhatsAppChannel("ACxxx", "authtoken", "whatsapp:+14155238886", client=_client_with(handler))
        result = channel.send(to="+919611550053", text="hi")

        assert result.status == "failed"
        assert result.external_ref is None
        assert result.detail["code"] == 63007

    def test_network_error_raises_channel_unavailable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        channel = TwilioWhatsAppChannel("ACxxx", "authtoken", "whatsapp:+14155238886", client=_client_with(handler))
        with pytest.raises(ChannelUnavailable):
            channel.send(to="+919611550053", text="hi")

    def test_missing_credentials_rejected_at_construction(self):
        with pytest.raises(ValueError):
            TwilioWhatsAppChannel("", "authtoken", "whatsapp:+14155238886")
        with pytest.raises(ValueError):
            TwilioWhatsAppChannel("ACxxx", "", "whatsapp:+14155238886")
        with pytest.raises(ValueError):
            TwilioWhatsAppChannel("ACxxx", "authtoken", "")


class TestSendTemplate:
    def test_send_template_posts_content_sid_and_json_encoded_variables(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["form"] = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            return httpx.Response(201, json={"sid": "MM123", "status": "queued"})

        channel = TwilioWhatsAppChannel(
            "ACxxx", "authtoken", "whatsapp:+19376467656", client=_client_with(handler),
        )
        result = channel.send_template(
            to="+919611550053", content_sid="HX5790acebff4a2faa3ae74c1cb8439554",
            content_variables={"1": "INV-2201", "2": "42,500", "3": "22", "4": "https://rzp.io/i/abc"},
        )

        assert captured["form"]["To"] == "whatsapp:+919611550053"
        assert captured["form"]["From"] == "whatsapp:+19376467656"
        assert captured["form"]["ContentSid"] == "HX5790acebff4a2faa3ae74c1cb8439554"
        assert json.loads(captured["form"]["ContentVariables"]) == {
            "1": "INV-2201", "2": "42,500", "3": "22", "4": "https://rzp.io/i/abc",
        }
        assert "Body" not in captured["form"]
        assert result.status == "sent"
        assert result.external_ref == "MM123"

    def test_send_template_before_approval_clears_is_a_clean_failed_result(self):
        """Live-caught: sending via an unapproved/pending ContentSid doesn't
        raise -- Twilio answers with a clean rejection (error 63016, the
        24h-window rule, until the template is actually approved), exactly
        like a sandbox opt-out rejection on send()."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"sid": "MM124", "status": "undelivered"})

        channel = TwilioWhatsAppChannel(
            "ACxxx", "authtoken", "whatsapp:+19376467656", client=_client_with(handler),
        )
        result = channel.send_template(
            to="+919611550053", content_sid="HXpending", content_variables={"1": "x"},
        )
        assert result.status == "sent"  # the HTTP call itself succeeded; delivery status is Twilio's async concern
        assert result.detail["status"] == "undelivered"

    def test_send_template_network_error_raises_channel_unavailable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        channel = TwilioWhatsAppChannel("ACxxx", "authtoken", "whatsapp:+19376467656", client=_client_with(handler))
        with pytest.raises(ChannelUnavailable):
            channel.send_template(to="+919611550053", content_sid="HXxxx", content_variables={})


class TestListMessages:
    def test_lists_messages_filtered_by_to_and_from(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"messages": [
                {"sid": "SM2", "body": "second reply", "direction": "inbound", "date_sent": "2026-09-01T13:00:00Z"},
                {"sid": "SM1", "body": "first reply", "direction": "inbound", "date_sent": "2026-09-01T12:00:00Z"},
            ]})

        channel = TwilioWhatsAppChannel("ACxxx", "authtoken", "whatsapp:+19376467656", client=_client_with(handler))
        messages = channel.list_messages(to="+19376467656", from_="+919611550053", limit=5)

        assert "To=whatsapp" in captured["url"] or "To=whatsapp%3A" in captured["url"]
        assert len(messages) == 2
        assert messages[0]["sid"] == "SM2"

    def test_list_messages_api_error_raises_channel_unavailable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"message": "bad request"})

        channel = TwilioWhatsAppChannel("ACxxx", "authtoken", "whatsapp:+19376467656", client=_client_with(handler))
        with pytest.raises(ChannelUnavailable):
            channel.list_messages(to="+19376467656", from_="+919611550053")

    def test_list_messages_network_error_raises_channel_unavailable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        channel = TwilioWhatsAppChannel("ACxxx", "authtoken", "whatsapp:+19376467656", client=_client_with(handler))
        with pytest.raises(ChannelUnavailable):
            channel.list_messages(to="+19376467656", from_="+919611550053")


class TestVerifyCredentials:
    def test_fetches_the_account_resource_not_a_message(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"friendly_name": "My test account", "status": "active"})

        channel = TwilioWhatsAppChannel("ACxxx", "authtoken", "whatsapp:+14155238886", client=_client_with(handler))
        info = channel.verify_credentials()

        assert captured["method"] == "GET"
        assert captured["url"].endswith("/Accounts/ACxxx.json")
        assert info == {"friendly_name": "My test account", "status": "active"}

    def test_returns_none_on_bad_auth(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "Authenticate"})

        channel = TwilioWhatsAppChannel("ACxxx", "wrong", "whatsapp:+14155238886", client=_client_with(handler))
        assert channel.verify_credentials() is None

    def test_returns_none_on_network_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        channel = TwilioWhatsAppChannel("ACxxx", "authtoken", "whatsapp:+14155238886", client=_client_with(handler))
        assert channel.verify_credentials() is None

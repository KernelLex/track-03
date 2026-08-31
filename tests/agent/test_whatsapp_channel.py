"""Tests for agent.notify.whatsapp -- no real network calls, no real Meta
token needed. Every request shape is exercised through httpx.MockTransport
(matching tests/agent/test_notify_channels.py's convention for the other
real channels), so a live WHATSAPP_ACCESS_TOKEN/WHATSAPP_PHONE_NUMBER_ID
swap is the only thing left to actually go live -- not a rewrite.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest

from agent.notify.protocol import ChannelUnavailable
from agent.notify.whatsapp import (
    IncomingWhatsAppMessage,
    WhatsAppChannel,
    parse_incoming_messages,
    verify_webhook_challenge,
    verify_webhook_signature,
)


def _client_with(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class TestSendTextMessage:
    def test_send_success_posts_expected_shape(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers["authorization"]
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "messaging_product": "whatsapp",
                    "contacts": [{"input": "919611550053", "wa_id": "919611550053"}],
                    "messages": [{"id": "wamid.ABC123"}],
                },
            )

        channel = WhatsAppChannel("1306946662503182", "test-token", transport=httpx.MockTransport(handler))
        result = channel.send(to="919611550053", text="Your invoice is ready")

        assert captured["url"].endswith("/v25.0/1306946662503182/messages")
        assert captured["auth"] == "Bearer test-token"
        assert captured["body"] == {
            "messaging_product": "whatsapp", "to": "919611550053",
            "type": "text", "text": {"body": "Your invoice is ready"},
        }
        assert result.status == "sent"
        assert result.channel == "whatsapp"
        assert result.external_ref == "wamid.ABC123"
        assert result.detail["wa_id"] == "919611550053"

    def test_outside_window_rejection_is_a_clean_failed_result(self):
        """Meta's real error for messaging outside the 24h session window --
        must not raise, since the API was reached and answered."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={"error": {"message": "Re-engagement message", "code": 131047, "type": "OAuthException"}},
            )

        channel = WhatsAppChannel("123", "test-token", client=_client_with(handler))
        result = channel.send(to="919611550053", text="hi")

        assert result.status == "failed"
        assert result.external_ref is None
        assert result.detail["code"] == 131047

    def test_network_error_raises_channel_unavailable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        channel = WhatsAppChannel("123", "test-token", client=_client_with(handler))
        with pytest.raises(ChannelUnavailable):
            channel.send(to="919611550053", text="hi")

    def test_missing_credentials_rejected_at_construction(self):
        with pytest.raises(ValueError):
            WhatsAppChannel("", "test-token")
        with pytest.raises(ValueError):
            WhatsAppChannel("123", "")

    def test_custom_api_version_is_used_in_url(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"messages": [{"id": "wamid.X"}], "contacts": [{"wa_id": "1"}]})

        channel = WhatsAppChannel("123", "tok", api_version="v20.0", client=_client_with(handler))
        channel.send(to="1", text="hi")
        assert "/v20.0/123/messages" in captured["url"]


class TestSendTemplate:
    def test_template_with_body_params_and_url_button_suffix(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"messages": [{"id": "wamid.T1"}], "contacts": [{"wa_id": "1"}]})

        channel = WhatsAppChannel("123", "tok", client=_client_with(handler))
        channel.send_template(
            to="919611550053", template_name="payment_reminder", language_code="en_US",
            body_params=["Rs 42,500", "22 days"], url_button_suffix="l/abc123",
        )

        template = captured["body"]["template"]
        assert template["name"] == "payment_reminder"
        assert template["language"] == {"code": "en_US"}
        body_component = next(c for c in template["components"] if c["type"] == "body")
        assert body_component["parameters"] == [{"type": "text", "text": "Rs 42,500"}, {"type": "text", "text": "22 days"}]
        button_component = next(c for c in template["components"] if c["type"] == "button")
        assert button_component == {
            "type": "button", "sub_type": "url", "index": "0",
            "parameters": [{"type": "text", "text": "l/abc123"}],
        }

    def test_template_with_no_button_omits_button_component(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"messages": [{"id": "wamid.T2"}], "contacts": [{"wa_id": "1"}]})

        channel = WhatsAppChannel("123", "tok", client=_client_with(handler))
        channel.send_template(to="1", template_name="mandate_repaired")

        template = captured["body"]["template"]
        assert "components" not in template


class TestSendInteractiveButtons:
    def test_posts_up_to_three_reply_buttons(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"messages": [{"id": "wamid.I1"}], "contacts": [{"wa_id": "1"}]})

        channel = WhatsAppChannel("123", "tok", client=_client_with(handler))
        channel.send_interactive_buttons(
            to="1", text="How would you like to proceed?",
            buttons=[("btn_paid", "I already paid"), ("btn_dispute", "I dispute this")],
        )

        action = captured["body"]["interactive"]["action"]
        assert action["buttons"] == [
            {"type": "reply", "reply": {"id": "btn_paid", "title": "I already paid"}},
            {"type": "reply", "reply": {"id": "btn_dispute", "title": "I dispute this"}},
        ]

    def test_rejects_zero_or_more_than_three_buttons(self):
        channel = WhatsAppChannel("123", "tok", client=_client_with(lambda r: httpx.Response(200, json={})))
        with pytest.raises(ValueError):
            channel.send_interactive_buttons(to="1", text="x", buttons=[])
        with pytest.raises(ValueError):
            channel.send_interactive_buttons(to="1", text="x", buttons=[("a", "A")] * 4)


class TestVerifyCredentials:
    def test_returns_phone_number_identity_on_success(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"display_phone_number": "+91 96115 50053", "verified_name": "TrueCommit"})

        channel = WhatsAppChannel("1306946662503182", "tok", client=_client_with(handler))
        info = channel.verify_credentials()
        assert info == {"display_phone_number": "+91 96115 50053", "verified_name": "TrueCommit"}

    def test_returns_none_on_bad_token(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": {"message": "Invalid OAuth access token"}})

        channel = WhatsAppChannel("123", "bad-token", client=_client_with(handler))
        assert channel.verify_credentials() is None

    def test_returns_none_on_network_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        channel = WhatsAppChannel("123", "tok", client=_client_with(handler))
        assert channel.verify_credentials() is None


class TestWebhookSignatureVerification:
    def test_valid_signature_verifies(self):
        body = b'{"object":"whatsapp_business_account"}'
        secret = "app-secret-123"
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert verify_webhook_signature(body, sig, secret) is True

    def test_tampered_body_fails_verification(self):
        secret = "app-secret-123"
        sig = "sha256=" + hmac.new(secret.encode(), b'{"a":1}', hashlib.sha256).hexdigest()
        assert verify_webhook_signature(b'{"a":2}', sig, secret) is False

    def test_missing_header_fails(self):
        assert verify_webhook_signature(b"body", None, "secret") is False

    def test_header_without_sha256_prefix_fails(self):
        assert verify_webhook_signature(b"body", "deadbeef", "secret") is False


class TestWebhookChallengeHandshake:
    def test_matching_token_echoes_challenge(self):
        result = verify_webhook_challenge(
            mode="subscribe", token="my-verify-token", challenge="12345", expected_token="my-verify-token",
        )
        assert result == "12345"

    def test_wrong_token_returns_none(self):
        result = verify_webhook_challenge(
            mode="subscribe", token="wrong", challenge="12345", expected_token="my-verify-token",
        )
        assert result is None

    def test_wrong_mode_returns_none(self):
        result = verify_webhook_challenge(
            mode="unsubscribe", token="my-verify-token", challenge="12345", expected_token="my-verify-token",
        )
        assert result is None


class TestParseIncomingMessages:
    def test_parses_free_text_message(self):
        payload = {
            "entry": [{"changes": [{"value": {"messages": [
                {"from": "919611550053", "id": "wamid.IN1", "type": "text", "text": {"body": "will pay tomorrow"}},
            ]}}]}],
        }
        messages = parse_incoming_messages(payload)
        assert len(messages) == 1
        msg = messages[0]
        assert isinstance(msg, IncomingWhatsAppMessage)
        assert msg.from_wa_id == "919611550053"
        assert msg.text == "will pay tomorrow"
        assert msg.is_structured_reply is False

    def test_parses_button_reply_as_structured(self):
        payload = {
            "entry": [{"changes": [{"value": {"messages": [
                {
                    "from": "919611550053", "id": "wamid.IN2", "type": "interactive",
                    "interactive": {"type": "button_reply", "button_reply": {"id": "btn_paid", "title": "I already paid"}},
                },
            ]}}]}],
        }
        messages = parse_incoming_messages(payload)
        assert len(messages) == 1
        msg = messages[0]
        assert msg.is_structured_reply is True
        assert msg.button_id == "btn_paid"
        assert msg.button_title == "I already paid"
        assert msg.text is None

    def test_status_only_delivery_yields_no_messages(self):
        """A delivery/read receipt callback has `statuses`, not `messages` --
        nothing to diagnose, must not crash or fabricate a message."""
        payload = {
            "entry": [{"changes": [{"value": {"statuses": [
                {"id": "wamid.OUT1", "status": "delivered", "recipient_id": "919611550053"},
            ]}}]}],
        }
        assert parse_incoming_messages(payload) == []

    def test_multiple_entries_and_changes_are_all_parsed(self):
        payload = {
            "entry": [
                {"changes": [{"value": {"messages": [{"from": "1", "id": "m1", "type": "text", "text": {"body": "a"}}]}}]},
                {"changes": [{"value": {"messages": [{"from": "2", "id": "m2", "type": "text", "text": {"body": "b"}}]}}]},
            ],
        }
        messages = parse_incoming_messages(payload)
        assert [m.from_wa_id for m in messages] == ["1", "2"]

    def test_unrecognized_message_type_is_skipped_not_crashed(self):
        payload = {
            "entry": [{"changes": [{"value": {"messages": [
                {"from": "1", "id": "m1", "type": "image", "image": {"id": "media123"}},
            ]}}]}],
        }
        assert parse_incoming_messages(payload) == []

    def test_empty_payload_yields_no_messages(self):
        assert parse_incoming_messages({}) == []

"""Twilio's webhook signature, and the proxy trap around it.

`/demo/whatsapp-webhook` is public and makes the system answer a debtor, so
a forged request must be rejected before anything reads the body. These
check the algorithm against Twilio's own published worked example rather
than against my implementation of it -- an algorithm that only agrees with
itself proves nothing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from agent.notify.twilio_signing import (
    expected_signature,
    public_url_for,
    verify_twilio_signature,
)

# Twilio's documented example (Security guide, "Validating signatures").
DOC_URL = "https://mycompany.com/myapp.php?foo=1&bar=2"
DOC_PARAMS = {
    "CallSid": "CA1234567890ABCDE",
    "Caller": "+12349013030",
    "Digits": "1234",
    "From": "+12349013030",
    "To": "+18005551212",
}
DOC_TOKEN = "12345"
DOC_SIGNATURE = "0/KCTR6DLpKmkAf8muzZqo1nDgQ="


class TestAgainstTwiliosPublishedExample:
    def test_it_reproduces_the_documented_signature(self):
        assert expected_signature(DOC_URL, DOC_PARAMS, DOC_TOKEN) == DOC_SIGNATURE

    def test_it_accepts_that_signature(self):
        assert verify_twilio_signature(
            url=DOC_URL, params=DOC_PARAMS, signature=DOC_SIGNATURE, auth_token=DOC_TOKEN)

    def test_parameter_order_does_not_matter(self):
        """Twilio sorts by key before concatenating, and a dict from a form
        parse has no guaranteed order."""
        shuffled = dict(reversed(list(DOC_PARAMS.items())))
        assert expected_signature(DOC_URL, shuffled, DOC_TOKEN) == DOC_SIGNATURE


class TestAgainstAnIndependentDerivation:
    """Stronger than the published vector alone: this rebuilds the digest
    from the algorithm's definition rather than calling the same function."""

    def test_it_matches_a_hand_built_hmac(self):
        payload = DOC_URL + "".join(k + DOC_PARAMS[k] for k in sorted(DOC_PARAMS))
        digest = hmac.new(DOC_TOKEN.encode(), payload.encode(), hashlib.sha1).digest()
        assert expected_signature(DOC_URL, DOC_PARAMS, DOC_TOKEN) == base64.b64encode(digest).decode()


class TestRejection:
    def test_a_tampered_parameter_fails(self):
        forged = {**DOC_PARAMS, "From": "+919999999999"}
        assert not verify_twilio_signature(
            url=DOC_URL, params=forged, signature=DOC_SIGNATURE, auth_token=DOC_TOKEN)

    def test_a_different_url_fails(self):
        assert not verify_twilio_signature(
            url=DOC_URL + "&evil=1", params=DOC_PARAMS,
            signature=DOC_SIGNATURE, auth_token=DOC_TOKEN)

    def test_the_wrong_token_fails(self):
        assert not verify_twilio_signature(
            url=DOC_URL, params=DOC_PARAMS, signature=DOC_SIGNATURE, auth_token="wrong")

    def test_a_missing_signature_is_a_failure_not_a_skip(self):
        """An endpoint that accepts an unsigned request whenever the header
        is absent is unauthenticated with extra steps. This project already
        shipped one fail-open guard (WHAT_BROKE #25); not twice."""
        assert not verify_twilio_signature(
            url=DOC_URL, params=DOC_PARAMS, signature=None, auth_token=DOC_TOKEN)
        assert not verify_twilio_signature(
            url=DOC_URL, params=DOC_PARAMS, signature="", auth_token=DOC_TOKEN)

    def test_a_missing_auth_token_fails_closed(self):
        assert not verify_twilio_signature(
            url=DOC_URL, params=DOC_PARAMS, signature=DOC_SIGNATURE, auth_token="")

    def test_an_empty_parameter_value_is_still_signed_over(self):
        """`params[key] or ""` must contribute the key, not vanish -- a
        parameter that disappears from the payload lets an attacker add
        empty fields freely."""
        with_empty = {**DOC_PARAMS, "Body": ""}
        assert expected_signature(DOC_URL, with_empty, DOC_TOKEN) != DOC_SIGNATURE


class TestThePublicUrlTrap:
    """Twilio signs the URL *it* requested -- https for any public
    deployment. Behind Render's TLS terminator the app sees http, so a naive
    `str(request.url)` fails every signature. Fails closed, which is safe,
    but for a reason that is not obvious from the symptom."""

    def test_forwarded_proto_restores_the_scheme_twilio_used(self):
        assert public_url_for(
            request_url="http://track-03.onrender.com/demo/whatsapp-webhook",
            forwarded_proto="https",
        ) == "https://track-03.onrender.com/demo/whatsapp-webhook"

    def test_it_takes_the_first_proto_when_several_proxies_appended_one(self):
        assert public_url_for(
            request_url="http://example.com/demo/whatsapp-webhook",
            forwarded_proto="https, http",
        ).startswith("https://")

    def test_an_explicit_base_url_wins_over_a_spoofable_header(self, monkeypatch):
        """The header comes from the request. An attacker controls it; the
        environment variable they cannot."""
        monkeypatch.setenv("TRUECOMMIT_PUBLIC_BASE_URL", "https://track-03.onrender.com")
        assert public_url_for(
            request_url="http://evil.internal/demo/whatsapp-webhook",
            forwarded_proto="http",
        ) == "https://track-03.onrender.com/demo/whatsapp-webhook"

    def test_an_explicit_base_url_without_a_scheme_is_assumed_https(self, monkeypatch):
        monkeypatch.setenv("TRUECOMMIT_PUBLIC_BASE_URL", "track-03.onrender.com")
        assert public_url_for(
            request_url="http://x/demo/whatsapp-webhook", forwarded_proto=None,
        ) == "https://track-03.onrender.com/demo/whatsapp-webhook"

    def test_a_trailing_slash_on_the_base_does_not_double_up(self, monkeypatch):
        monkeypatch.setenv("TRUECOMMIT_PUBLIC_BASE_URL", "https://track-03.onrender.com/")
        assert public_url_for(
            request_url="http://x/demo/whatsapp-webhook", forwarded_proto=None,
        ) == "https://track-03.onrender.com/demo/whatsapp-webhook"

    def test_it_never_lets_the_base_url_change_the_path(self, monkeypatch):
        """Only scheme and host are replaced. If a base URL could also
        rewrite the path, verification could be pointed at a route the
        request never touched."""
        monkeypatch.setenv("TRUECOMMIT_PUBLIC_BASE_URL", "https://real.example/some/other/path")
        assert public_url_for(
            request_url="http://x/demo/whatsapp-webhook", forwarded_proto=None,
        ) == "https://real.example/demo/whatsapp-webhook"

    def test_the_query_string_survives(self):
        """It is part of what Twilio signed."""
        assert public_url_for(
            request_url="http://x/demo/whatsapp-webhook?a=1&b=2", forwarded_proto="https",
        ) == "https://x/demo/whatsapp-webhook?a=1&b=2"

    def test_with_no_proxy_hint_the_url_is_unchanged(self, monkeypatch):
        monkeypatch.delenv("TRUECOMMIT_PUBLIC_BASE_URL", raising=False)
        url = "https://example.com/demo/whatsapp-webhook"
        assert public_url_for(request_url=url, forwarded_proto=None) == url


class TestEndToEndAgainstARealisticTwilioPost:
    """The parameters Twilio actually sends for an inbound WhatsApp message,
    signed and verified the way the endpoint will do it."""

    URL = "https://track-03.onrender.com/demo/whatsapp-webhook"
    TOKEN = "an-account-auth-token"
    FORM = {
        "SmsMessageSid": "SM1111111111111111111111111111111",
        "MessageSid": "SM1111111111111111111111111111111",
        "AccountSid": "AC00000000000000000000000000000000",
        "From": "whatsapp:+919611550053",
        "To": "whatsapp:+19376467656",
        "Body": "I can pay 21,000 on the 5th",
        "NumMedia": "0",
        "ProfileName": "Amogh",
        "WaId": "919611550053",
    }

    @pytest.fixture
    def signature(self):
        return expected_signature(self.URL, self.FORM, self.TOKEN)

    def test_a_genuine_delivery_verifies(self, signature):
        assert verify_twilio_signature(
            url=self.URL, params=self.FORM, signature=signature, auth_token=self.TOKEN)

    def test_an_attacker_cannot_swap_the_body(self, signature):
        """The specific attack this endpoint exists to stop: forging a reply
        so the system answers a message the debtor never sent."""
        forged = {**self.FORM, "Body": "I will pay the full amount today"}
        assert not verify_twilio_signature(
            url=self.URL, params=forged, signature=signature, auth_token=self.TOKEN)

    def test_an_attacker_cannot_swap_the_sender(self, signature):
        forged = {**self.FORM, "From": "whatsapp:+910000000000"}
        assert not verify_twilio_signature(
            url=self.URL, params=forged, signature=signature, auth_token=self.TOKEN)

    def test_verification_survives_the_proxy_rewrite(self, signature):
        """The whole point of public_url_for: the app sees http, Twilio
        signed https, and the signature must still verify."""
        rebuilt = public_url_for(
            request_url="http://track-03.onrender.com/demo/whatsapp-webhook",
            forwarded_proto="https",
        )
        assert verify_twilio_signature(
            url=rebuilt, params=self.FORM, signature=signature, auth_token=self.TOKEN)

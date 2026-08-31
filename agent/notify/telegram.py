"""Telegram Bot API channel. Free to send, no per-message cost — the
messaging channel this project actually uses to reach a debtor, rather than
a paid SMS/WhatsApp API. DEVDOC_v6's channel set (agent.bounds.context.
ALL_CHANNELS) includes "telegram" alongside sms/email/whatsapp/ivr.

One real constraint worth being explicit about: unlike SMS, a bot cannot
cold-message an arbitrary phone number. The recipient must have started a
conversation with the bot first (or been added to a group the bot is in),
which is how Telegram gets the numeric chat_id this module sends to. See
tools/telegram_get_chat_id.py for finding that id during testing, and
docs/CHANNELS.md for how this shapes what the live demo can show.
"""

from __future__ import annotations

import httpx

from agent.notify.protocol import ChannelUnavailable, MessageSendResult

TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramChannel:
    channel_tag = "telegram"

    def __init__(self, bot_token: str, *, client: httpx.Client | None = None, timeout: float = 10.0):
        if not bot_token:
            raise ValueError("TelegramChannel requires a non-empty bot_token")
        self._token = bot_token
        self._client = client or httpx.Client(timeout=timeout)
        self._base = f"{TELEGRAM_API_BASE}/bot{bot_token}"

    def send(self, *, to: str, text: str) -> MessageSendResult:
        """`to` is a Telegram chat_id (numeric, as a string) — not a phone
        number. Raises ChannelUnavailable only when the call itself couldn't
        be completed (network error); a Telegram-side rejection (bot blocked,
        chat not found) comes back as a clean status="failed" result instead,
        since that's Telegram's API answering, not an unknown outcome."""
        try:
            response = self._client.post(f"{self._base}/sendMessage", json={"chat_id": to, "text": text})
        except httpx.HTTPError as exc:
            raise ChannelUnavailable("telegram", str(exc)) from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise ChannelUnavailable("telegram", f"non-JSON response: {exc}") from exc

        if response.status_code != 200 or not body.get("ok"):
            return MessageSendResult(
                channel="telegram",
                external_ref=None,
                status="failed",
                detail={"status_code": response.status_code, "description": body.get("description")},
            )

        result = body["result"]
        return MessageSendResult(
            channel="telegram",
            external_ref=str(result["message_id"]),
            status="sent",
            detail={"chat_id": result["chat"]["id"], "date": result["date"]},
        )

    def get_me(self) -> dict:
        """Confirms the bot token is valid and returns the bot's own
        identity (id, username) -- read-only, no message sent, for a quick
        credential check (tools/verify_credentials.py)."""
        try:
            response = self._client.get(f"{self._base}/getMe")
        except httpx.HTTPError as exc:
            raise ChannelUnavailable("telegram", str(exc)) from exc
        body = response.json()
        if not body.get("ok"):
            raise ChannelUnavailable("telegram", body.get("description", "getMe failed"))
        return body["result"]

    def get_updates(self, *, offset: int | None = None) -> list[dict]:
        """Thin wrapper over getUpdates — used by tools/telegram_get_chat_id.py
        and agent/api/demo.py's reply-polling endpoint. Without `offset`,
        Telegram returns its full retained backlog every time; passing the
        highest `update_id` seen so far plus one returns only updates newer
        than that, which is what makes repeated polling for "did they reply
        yet" cheap and non-redundant rather than re-scanning history."""
        params = {"offset": offset} if offset is not None else {}
        try:
            response = self._client.get(f"{self._base}/getUpdates", params=params)
        except httpx.HTTPError as exc:
            raise ChannelUnavailable("telegram", str(exc)) from exc
        body = response.json()
        if not body.get("ok"):
            raise ChannelUnavailable("telegram", body.get("description", "getUpdates failed"))
        return body["result"]

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "TelegramChannel":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

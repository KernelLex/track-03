#!/usr/bin/env python3
"""Find the chat_id(s) TrueCommit can send to. docs/CHANNELS.md.

    TELEGRAM_BOT_TOKEN=xxx uv run python tools/telegram_get_chat_id.py

A Telegram bot can't cold-message a phone number — it can only message a
chat_id that has already messaged *it* first (or added it to a group). So:

    1. Open Telegram, search for your bot by the username @BotFather gave it.
    2. Send it any message ("hi").
    3. Run this script — it prints the chat_id(s) from whoever has messaged
       the bot so far, newest first.

That chat_id is what goes in the `to` field TelegramChannel.send() expects —
it is not a phone number and not a username.
"""

from __future__ import annotations

import os
import sys

from agent.notify.protocol import ChannelUnavailable
from agent.notify.telegram import TelegramChannel


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print(
            "TELEGRAM_BOT_TOKEN must be set — create a bot via @BotFather on Telegram, "
            "it gives you a token immediately, no approval wait.",
            file=sys.stderr,
        )
        sys.exit(1)

    channel = TelegramChannel(token)
    try:
        updates = channel.get_updates()
    except ChannelUnavailable as exc:
        print(f"Could not reach Telegram: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        channel.close()

    if not updates:
        print(
            "No messages yet. Open your bot in Telegram and send it anything, "
            "then re-run this script."
        )
        return

    seen: dict[int, str] = {}
    for update in reversed(updates):
        message = update.get("message") or update.get("channel_post")
        if not message:
            continue
        chat = message["chat"]
        label = chat.get("username") or chat.get("first_name") or chat.get("title") or "(no name)"
        seen[chat["id"]] = label

    print("chat_id      from")
    for chat_id, label in seen.items():
        print(f"{chat_id:<12} {label}")


if __name__ == "__main__":
    main()

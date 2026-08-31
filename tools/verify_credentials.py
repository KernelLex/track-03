#!/usr/bin/env python3
"""Live credential verification -- one safe, mostly side-effect-free check
per service. docs/CHANNELS.md, docs/LLM_EXTRACTION.md.

    uv run python tools/verify_credentials.py

Never prints a secret value -- only derived, safe-to-show identity info (a
bot's username, a Twilio account's friendly_name/status, an extraction
result). Skips, rather than fails, a service whose env vars aren't set, so
this is safe to run at any point with partial credentials.

No message is sent and no call is placed: Telegram uses getMe/getUpdates,
Twilio uses the read-only account-fetch endpoint (verify_credentials()),
and Anthropic makes two small real extraction calls (the one live side
effect here, each a few cents) since there's no read-only equivalent for
"does this API key work" that also proves the actual code path functions.
"""

from __future__ import annotations

import os
import sys


def check_telegram() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("[telegram]  skipped -- TELEGRAM_BOT_TOKEN not set")
        return

    from agent.notify.protocol import ChannelUnavailable
    from agent.notify.telegram import TelegramChannel

    channel = TelegramChannel(token)
    try:
        me = channel.get_me()
        print(f"[telegram]  OK -- bot identity confirmed: @{me.get('username')} (id={me.get('id')})")
        updates = channel.get_updates()
        chats: dict[int, str] = {}
        for update in reversed(updates):
            message = update.get("message") or update.get("channel_post")
            if not message:
                continue
            chat = message["chat"]
            chats[chat["id"]] = chat.get("username") or chat.get("first_name") or chat.get("title") or "(no name)"
        if chats:
            print(f"[telegram]  {len(chats)} chat_id(s) available to send to:")
            for chat_id, label in chats.items():
                print(f"[telegram]    {chat_id}  {label}")
        else:
            print("[telegram]  no chat_id available yet -- message the bot first, then re-run "
                  "(or tools/telegram_get_chat_id.py)")
    except ChannelUnavailable as exc:
        print(f"[telegram]  FAILED -- {exc}")
    finally:
        channel.close()


def check_twilio() -> None:
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    from_number = os.environ.get("TWILIO_FROM_NUMBER")
    api_key_sid = os.environ.get("TWILIO_API_KEY_SID")
    api_key_secret = os.environ.get("TWILIO_API_KEY_SECRET")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")

    if not sid or not from_number:
        print("[twilio]    skipped -- TWILIO_ACCOUNT_SID / TWILIO_FROM_NUMBER not set")
        return

    if api_key_sid and api_key_secret:
        secret, username = api_key_secret, api_key_sid
    elif auth_token:
        secret, username = auth_token, None
    else:
        print("[twilio]    skipped -- neither TWILIO_AUTH_TOKEN nor "
              "TWILIO_API_KEY_SID/TWILIO_API_KEY_SECRET is set")
        return

    from agent.notify.twilio_voice import TwilioVoiceChannel

    channel = TwilioVoiceChannel(sid, secret, from_number, auth_username=username)
    try:
        info = channel.verify_credentials()
    finally:
        channel.close()

    if info is None:
        print("[twilio]    FAILED -- could not authenticate against the account resource")
    else:
        print(f"[twilio]    OK -- account {info['friendly_name']!r}, status={info['status']}")
        print("[twilio]    (read-only check -- no call was placed, no cost incurred)")


def check_anthropic() -> None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("[anthropic] skipped -- ANTHROPIC_API_KEY not set")
        return

    from agent.diagnose.llm_extract import ExtractionFailed, extract_from_reply
    from agent.spend import BudgetExceeded, SpendLedger

    ledger = SpendLedger()
    print(f"[anthropic] spend so far: ${ledger.total_spent_usd():.4f} "
          f"(${ledger.remaining_budget_usd():.4f} remaining of the ${20:.2f} ceiling)")

    samples = [
        "We will pay the full amount by October 1st, funds are just clearing on our end.",
        "This invoice bills 200 units but we only received 150 -- we're disputing the difference.",
    ]
    for sample in samples:
        try:
            result = extract_from_reply(sample, spend_ledger=ledger)
            print(
                f"[anthropic] OK -- family={result.family.value} class={result.class_.value} "
                f"confidence={result.confidence:.2f}  <- {sample[:50]!r}..."
            )
        except BudgetExceeded as exc:
            print(f"[anthropic] REFUSED -- {exc}")
            break
        except ExtractionFailed as exc:
            print(f"[anthropic] FAILED -- {exc}")
            if "anthropic-workspace-id" in str(exc):
                print(
                    "[anthropic]   This key is identity-linked and needs a workspace named "
                    "explicitly. Either set ANTHROPIC_WORKSPACE_ID (Console -> Workspaces -> "
                    "copy its id, wrkspc_...) or generate a plain workspace-scoped API key "
                    "instead, which doesn't need this at all."
                )
            break  # the second sample would fail identically -- no need to spend it

    print(f"[anthropic] spend after this run: ${ledger.total_spent_usd():.4f} "
          f"(${ledger.remaining_budget_usd():.4f} remaining)")


def main() -> int:
    print("Live credential verification -- never prints a secret value.\n")
    check_telegram()
    check_twilio()
    check_anthropic()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Minimal FastAPI app: the webhook receiver + a health check. DEVDOC_v6 §19.

Not the full dashboard DEVDOC_v6 envisions (Jinja templates, a human-queue
UI) — just enough that INGEST and LISTEN have a real HTTP endpoint to run
behind, since neither stage means anything without one. Wires directly into
the modules already built and tested in isolation
(`agent.ingest.webhooks.verify_and_ingest`, `agent.ingest.listen
.facts_from_webhook`) rather than reimplementing anything here.

Configuring a real Razorpay webhook against this endpoint needs a publicly
reachable URL and manual setup in the Razorpay dashboard — both outside
what this build can do unattended. See docs/LIMITATIONS.md.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request

from agent.ingest.listen import UnrecognizedWebhookEvent, facts_from_webhook
from agent.ingest.webhooks import EventStore, MalformedWebhook, SignatureInvalid, verify_and_ingest


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    db_path = os.environ.get("TRUECOMMIT_EVENTS_DB", "events.db")
    app.state.event_store = EventStore(db_path)
    try:
        yield
    finally:
        app.state.event_store.close()


app = FastAPI(title="TrueCommit", lifespan=lifespan)


def _webhook_secret_for(source: str) -> str:
    """One shared secret per source, from the environment. Raises rather than
    accepting an unverifiable webhook when unconfigured."""
    env_var = f"TRUECOMMIT_WEBHOOK_SECRET_{source.upper()}"
    secret = os.environ.get(env_var)
    if not secret:
        raise HTTPException(status_code=500, detail=f"no webhook secret configured for source={source!r} (set {env_var})")
    return secret


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhooks/{source}")
async def receive_webhook(source: str, request: Request) -> dict[str, object]:
    body = await request.body()
    signature = request.headers.get("x-razorpay-signature") or request.headers.get("x-webhook-signature")
    if not signature:
        raise HTTPException(status_code=400, detail="missing signature header")

    secret = _webhook_secret_for(source)

    try:
        result = verify_and_ingest(
            store=request.app.state.event_store, source=source, body=body, signature=signature, secret=secret,
        )
    except SignatureInvalid as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MalformedWebhook as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if result.is_duplicate:
        return {"status": "duplicate", "event_id": result.event_id}

    try:
        facts = facts_from_webhook(result)
    except UnrecognizedWebhookEvent:
        # Signature valid, redelivery-deduped -- LISTEN just has no extraction
        # rule for this event_type yet. 200: the sender shouldn't retry a
        # delivery this endpoint understood and recorded just fine.
        return {"status": "ingested_unrecognized_event", "event_id": result.event_id, "event_type": result.event_type}

    return {"status": "ingested", "event_id": result.event_id, "facts": [f.name for f in facts]}

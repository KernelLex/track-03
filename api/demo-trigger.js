// Vercel Function (Web Standard Request/Response, zero-config /api/*.js
// convention) -- proxies the frontend's live-trigger button to the real
// Render backend's POST /demo/trigger, attaching DEMO_TRIGGER_SECRET
// server-side. A Vercel Function's own env vars never reach the browser,
// unlike a static page's JS -- see netlify/functions/demo-trigger.js for
// the identical Netlify version of this same fix (kept in sync; the
// frontend calls the platform-neutral /api/demo-trigger path either way,
// see docs/DEMO_UI.md for how each platform routes that path).
//
// The browser sends only { channel, scenario } -- no secret. This adds
// the real secret and forwards to BACKEND_URL/demo/trigger, then relays
// the backend's response (status code included) straight back.

const BACKEND_URL = process.env.BACKEND_URL || 'https://track-03.onrender.com';
const DEMO_TRIGGER_SECRET = process.env.DEMO_TRIGGER_SECRET;

export async function POST(request) {
  // Same-origin check: only forward requests whose Origin matches this
  // site's own host. Not unspoofable (a scripted, non-browser request can
  // set any Origin header it wants), but it blocks casual cross-site abuse
  // from another page's fetch() call, which is the realistic threat for a
  // link that's only ever meant to be opened directly.
  const origin = request.headers.get('origin') || '';
  const host = request.headers.get('host') || '';
  if (host && origin && !origin.endsWith(host) && !origin.includes('localhost')) {
    return Response.json({ detail: 'origin not allowed' }, { status: 403 });
  }

  if (!DEMO_TRIGGER_SECRET) {
    return Response.json({ detail: 'DEMO_TRIGGER_SECRET not configured on this Vercel project' }, { status: 503 });
  }

  let payload;
  try {
    payload = await request.json();
  } catch (e) {
    return Response.json({ detail: 'invalid JSON body' }, { status: 400 });
  }

  try {
    const res = await fetch(BACKEND_URL.replace(/\/$/, '') + '/demo/trigger', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        secret: DEMO_TRIGGER_SECRET,
        channel: payload.channel,
        scenario: payload.scenario,
        // `to` is forwarded, and was not until 2026-09-02. The dashboard has
        // a "message my own number" field, the browser sent it, and this
        // proxy quietly dropped it -- so on a deployed site that field did
        // nothing and every call went to the server's own configured
        // contact. The backend validates it as E.164 and applies a
        // per-number cooldown; this only has to stop discarding it.
        to: payload.to || undefined,
      }),
    });
    const body = await res.text();
    return new Response(body, {
      status: res.status,
      headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store, no-cache, must-revalidate' },
    });
  } catch (e) {
    return Response.json({ detail: 'could not reach backend: ' + String(e) }, { status: 502 });
  }
}

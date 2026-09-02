// Vercel Function — proxies the dashboard's subscription failure buttons to
// the backend's POST /demo/subscription-alert, attaching DEMO_TRIGGER_SECRET
// server-side so it never reaches the browser. Same shape and same reasoning
// as api/demo-trigger.js; netlify/functions/demo-subscription-alert.js is the
// identical Netlify version, kept in sync.
//
// The browser sends { failure, to, call } and no secret. `failure` names
// which mandate defect to demonstrate — the six are genuinely different
// failures with different repairs, so the caller picks one rather than
// getting whichever happens to be first.

const BACKEND_URL = process.env.BACKEND_URL || 'https://track-03.onrender.com';
const DEMO_TRIGGER_SECRET = process.env.DEMO_TRIGGER_SECRET;

export async function POST(request) {
  // Same-origin check: blocks casual cross-site abuse from another page's
  // fetch(). Not unspoofable — a scripted request can set any Origin — but
  // that is the realistic threat for a link only ever opened directly.
  const origin = request.headers.get('origin') || '';
  const host = request.headers.get('host') || '';
  if (host && origin && !origin.endsWith(host) && !origin.includes('localhost')) {
    return Response.json({ detail: 'origin not allowed' }, { status: 403 });
  }

  if (!DEMO_TRIGGER_SECRET) {
    return Response.json(
      { detail: 'DEMO_TRIGGER_SECRET not configured on this Vercel project' },
      { status: 503 },
    );
  }

  let payload;
  try {
    payload = await request.json();
  } catch (e) {
    return Response.json({ detail: 'invalid JSON body' }, { status: 400 });
  }

  try {
    const res = await fetch(BACKEND_URL.replace(/\/$/, '') + '/demo/subscription-alert', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        secret: DEMO_TRIGGER_SECRET,
        failure: payload.failure,
        // The number someone watching wants the call on. Validated as E.164
        // by the backend, with a per-number cooldown — this only carries it.
        to: payload.to || undefined,
        call: payload.call !== false,
      }),
    });
    const body = await res.text();
    return new Response(body, {
      status: res.status,
      headers: {
        'Content-Type': 'application/json',
        // Edge caches POSTs otherwise — caught live on Netlify with an
        // Age header on a trigger response.
        'Cache-Control': 'no-store, no-cache, must-revalidate',
      },
    });
  } catch (e) {
    return Response.json({ detail: 'could not reach backend: ' + String(e) }, { status: 502 });
  }
}

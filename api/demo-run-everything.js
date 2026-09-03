// Vercel Function -- proxies the dashboard's "run the whole thing" button to
// the Render backend's POST /demo/run-everything, attaching
// DEMO_TRIGGER_SECRET server-side. Identical in shape and intent to
// demo-trigger.js; see netlify/functions/demo-run-everything.js for the
// Netlify twin, kept in sync.
//
// The secret never reaches the browser. That is the whole reason this file
// exists rather than the page calling the backend directly -- a static
// page's JS is readable by anyone who opens devtools, a Function's env is
// not.

const BACKEND_URL = process.env.BACKEND_URL || 'https://track-03.onrender.com';
const DEMO_TRIGGER_SECRET = process.env.DEMO_TRIGGER_SECRET;

export async function POST(request) {
  // Same-origin check, same caveat as demo-trigger.js: a scripted request
  // can set any Origin it likes, so this blocks casual cross-site abuse
  // rather than a determined caller.
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
    const res = await fetch(BACKEND_URL.replace(/\/$/, '') + '/demo/run-everything', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        secret: DEMO_TRIGGER_SECRET,
        // Both recipients are forwarded explicitly. demo-trigger.js dropped
        // `to` for two weeks and the dashboard's number field silently did
        // nothing (docs/WHAT_BROKE.md #23) -- so every field this proxy
        // carries is named here rather than spread from the payload.
        to: payload.to || undefined,
        telegram_chat_id: payload.telegram_chat_id || undefined,
        scenario: payload.scenario || 'b2b',
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

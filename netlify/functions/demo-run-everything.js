// Proxies the dashboard's "run the whole thing" button to the Render
// backend's POST /demo/run-everything, attaching DEMO_TRIGGER_SECRET
// server-side (a Netlify Function's own env vars never reach the browser,
// unlike a static page's JS). The Netlify twin of api/demo-run-everything.js
// -- kept in sync; the frontend calls the platform-neutral
// /api/demo-run-everything path either way, see docs/DEMO_UI.md.

const BACKEND_URL = process.env.BACKEND_URL || 'https://track-03.onrender.com';
const DEMO_TRIGGER_SECRET = process.env.DEMO_TRIGGER_SECRET;

exports.handler = async (event) => {
  if (event.httpMethod !== 'POST') {
    return { statusCode: 405, body: JSON.stringify({ detail: 'method not allowed' }) };
  }

  // Same-origin check, same caveat as demo-trigger.js: a scripted request
  // can set any Origin it likes, so this blocks casual cross-site abuse
  // rather than a determined caller.
  const origin = event.headers.origin || '';
  const host = event.headers.host || '';
  if (host && origin && !origin.endsWith(host) && !origin.includes('localhost')) {
    return { statusCode: 403, body: JSON.stringify({ detail: 'origin not allowed' }) };
  }

  if (!DEMO_TRIGGER_SECRET) {
    return { statusCode: 503, body: JSON.stringify({ detail: 'DEMO_TRIGGER_SECRET not configured on this Netlify site' }) };
  }

  let payload;
  try {
    payload = JSON.parse(event.body || '{}');
  } catch (e) {
    return { statusCode: 400, body: JSON.stringify({ detail: 'invalid JSON body' }) };
  }

  try {
    const res = await fetch(BACKEND_URL.replace(/\/$/, '') + '/demo/run-everything', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        secret: DEMO_TRIGGER_SECRET,
        // Both recipients named explicitly rather than spread. demo-trigger.js
        // dropped `to` for two weeks and the dashboard's number field
        // silently did nothing (docs/WHAT_BROKE.md #23).
        to: payload.to || undefined,
        telegram_chat_id: payload.telegram_chat_id || undefined,
        scenario: payload.scenario || 'b2b',
      }),
    });
    const body = await res.text();
    // Netlify's edge was caching demo-trigger's responses (observed Age: 11
    // on an identical-looking POST), which for a trigger endpoint means a
    // cached "sent" replayed without a real second send. Same explicit
    // no-store headers here.
    return {
      statusCode: res.status,
      headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store, no-cache, must-revalidate', 'Netlify-CDN-Cache-Control': 'no-store' },
      body,
    };
  } catch (e) {
    return { statusCode: 502, body: JSON.stringify({ detail: 'could not reach backend: ' + String(e) }) };
  }
};

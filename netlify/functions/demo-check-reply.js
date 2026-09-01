// Same proxy pattern as demo-trigger.js, for POST /demo/check-reply --
// see that file's header comment for why this exists as a Function
// rather than a direct browser-to-Render call with the secret attached.

const BACKEND_URL = process.env.BACKEND_URL || 'https://track-03.onrender.com';
const DEMO_TRIGGER_SECRET = process.env.DEMO_TRIGGER_SECRET;

exports.handler = async (event) => {
  if (event.httpMethod !== 'POST') {
    return { statusCode: 405, body: JSON.stringify({ detail: 'method not allowed' }) };
  }

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
    const res = await fetch(BACKEND_URL.replace(/\/$/, '') + '/demo/check-reply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        secret: DEMO_TRIGGER_SECRET,
        after_update_id: payload.after_update_id ?? null,
        diagnose: payload.diagnose !== false,
      }),
    });
    const body = await res.text();
    // Live-caught: this endpoint is polled every 3s with an often-identical
    // body (after_update_id stays null until a reply lands) -- exactly the
    // shape a caching layer serves stale, and Netlify's edge was observed
    // doing exactly that (Age: 11 on what should be a live, per-call check).
    // Explicit no-store so every poll actually reaches Render.
    return {
      statusCode: res.status,
      headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store, no-cache, must-revalidate', 'Netlify-CDN-Cache-Control': 'no-store' },
      body,
    };
  } catch (e) {
    return { statusCode: 502, body: JSON.stringify({ detail: 'could not reach backend: ' + String(e) }) };
  }
};

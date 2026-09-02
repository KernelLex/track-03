// Proxies the dashboard's subscription failure buttons to the real Render backend's
// POST /demo/subscription-alert, attaching DEMO_TRIGGER_SECRET server-side (a Netlify
// Function's own env vars never reach the browser, unlike a static page's
// JS -- this is the actual fix for the secret-in-public-source problem,
// not the "auto-fill it and accept the tradeoff" version shipped earlier).
//
// The browser sends { failure, to, call } -- no secret. This function
// adds the real secret and forwards to BACKEND_URL/demo/subscription-alert, then
// relays the backend's response (status code included) straight back.

const BACKEND_URL = process.env.BACKEND_URL || 'https://track-03.onrender.com';
const DEMO_TRIGGER_SECRET = process.env.DEMO_TRIGGER_SECRET;

exports.handler = async (event) => {
  if (event.httpMethod !== 'POST') {
    return { statusCode: 405, body: JSON.stringify({ detail: 'method not allowed' }) };
  }

  // Same-origin check: only forward requests whose Origin matches this
  // site's own host. Not unspoofable (a scripted, non-browser request can
  // set any Origin header it wants), but it blocks casual cross-site abuse
  // from another page's fetch() call, which is the realistic threat for a
  // link that's only ever meant to be opened directly.
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
    const res = await fetch(BACKEND_URL.replace(/\/$/, '') + '/demo/subscription-alert', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        secret: DEMO_TRIGGER_SECRET,
        failure: payload.failure,
        call: payload.call !== false,
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
    // Live-caught: Netlify's edge was caching this Function's response
    // (observed Age: 11 on an identical-looking POST) -- disastrous for a
    // trigger endpoint, since a cached "sent" could be replayed without a
    // real second send. Explicit no-store headers on every response.
    return {
      statusCode: res.status,
      headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store, no-cache, must-revalidate', 'Netlify-CDN-Cache-Control': 'no-store' },
      body,
    };
  } catch (e) {
    return { statusCode: 502, body: JSON.stringify({ detail: 'could not reach backend: ' + String(e) }) };
  }
};

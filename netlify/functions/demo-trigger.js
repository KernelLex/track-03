// Proxies the frontend's live-trigger button to the real Render backend's
// POST /demo/trigger, attaching DEMO_TRIGGER_SECRET server-side (a Netlify
// Function's own env vars never reach the browser, unlike a static page's
// JS -- this is the actual fix for the secret-in-public-source problem,
// not the "auto-fill it and accept the tradeoff" version shipped earlier).
//
// The browser sends only { channel, scenario } -- no secret. This function
// adds the real secret and forwards to BACKEND_URL/demo/trigger, then
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
    const res = await fetch(BACKEND_URL.replace(/\/$/, '') + '/demo/trigger', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        secret: DEMO_TRIGGER_SECRET,
        channel: payload.channel,
        scenario: payload.scenario,
      }),
    });
    const body = await res.text();
    return { statusCode: res.status, headers: { 'Content-Type': 'application/json' }, body };
  } catch (e) {
    return { statusCode: 502, body: JSON.stringify({ detail: 'could not reach backend: ' + String(e) }) };
  }
};

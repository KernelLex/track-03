// Netlify Function -- proxies the dashboard's approve/reject buttons to the
// Render backend's POST /demo/approvals/{id}/decide, attaching
// DEMO_TRIGGER_SECRET server-side. The Netlify twin of api/demo-approve.js.

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
  try { payload = JSON.parse(event.body || '{}'); }
  catch (e) { return { statusCode: 400, body: JSON.stringify({ detail: 'invalid JSON body' }) }; }
  if (!payload.approval_id) {
    return { statusCode: 400, body: JSON.stringify({ detail: 'approval_id is required' }) };
  }

  try {
    const res = await fetch(
      BACKEND_URL.replace(/\/$/, '') + '/demo/approvals/' + encodeURIComponent(payload.approval_id) + '/decide',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          secret: DEMO_TRIGGER_SECRET,
          decision: payload.decision,
          message: payload.message || undefined,
          note: payload.note || undefined,
        }),
      });
    const body = await res.text();
    return {
      statusCode: res.status,
      headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store, no-cache, must-revalidate', 'Netlify-CDN-Cache-Control': 'no-store' },
      body,
    };
  } catch (e) {
    return { statusCode: 502, body: JSON.stringify({ detail: 'could not reach backend: ' + String(e) }) };
  }
};

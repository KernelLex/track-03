// Vercel Function -- proxies the dashboard's approve/reject buttons to the
// Render backend's POST /demo/approvals/{id}/decide, attaching
// DEMO_TRIGGER_SECRET server-side. See netlify/functions/demo-approve.js for
// the Netlify twin, kept in sync.
//
// A human decision that sends a real message to a real debtor goes through
// the same secret-holding proxy as every other triggering call -- the
// browser never holds the secret.

const BACKEND_URL = process.env.BACKEND_URL || 'https://track-03.onrender.com';
const DEMO_TRIGGER_SECRET = process.env.DEMO_TRIGGER_SECRET;

export async function POST(request) {
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
  if (!payload.approval_id) {
    return Response.json({ detail: 'approval_id is required' }, { status: 400 });
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
    return new Response(body, {
      status: res.status,
      headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store, no-cache, must-revalidate' },
    });
  } catch (e) {
    return Response.json({ detail: 'could not reach backend: ' + String(e) }, { status: 502 });
  }
}

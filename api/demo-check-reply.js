// Vercel Function -- same proxy pattern as demo-trigger.js, for POST
// /demo/check-reply. See that file's header comment for why this exists.

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
    return new Response(body, {
      status: res.status,
      headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store, no-cache, must-revalidate' },
    });
  } catch (e) {
    return Response.json({ detail: 'could not reach backend: ' + String(e) }, { status: 502 });
  }
}

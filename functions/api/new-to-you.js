// Cloudflare Pages Function — POST /api/new-to-you
// ---------------------------------------------------------------------------
// Counts answers to "How much of this did you already know?" on theme pages. The browser posts
// { page, answer } (see site/theme.js). Counts only: no personal data, no IP, no cookies.
//
// Each count is one key in a Workers KV store, tagged with the page, the answer and the month,
// e.g.  "/impact/work/ · Some of it · 2026-11"  →  "12"
// Read the counts in the Cloudflare dashboard: Storage & Databases → KV → the store → its keys.
//
// --- Required binding (Pages project → Settings → Bindings → KV namespace) ---
//   NEW_TO_YOU   The KV store. If it isn't bound, answers are dropped (the page still says thanks).
//
// KV isn't built for exact counting: two clicks in the same second can count as one. Fine for
// a rough read of how pages land.

const ANSWERS = new Set(["All of it", "Some of it", "None of it"]);
const PAGE_RE = /^\/[a-z0-9\-\/]{0,100}$/;
const MAX_BODY_BYTES = 1024;

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });

export async function onRequestPost({ request, env }) {
  let body;
  try {
    const raw = await request.text();
    if (raw.length > MAX_BODY_BYTES) return json({ ok: false }, 413);
    body = JSON.parse(raw || "{}");
  } catch {
    return json({ ok: false }, 400);
  }

  const page = String(body.page || "");
  const answer = String(body.answer || "");
  if (!PAGE_RE.test(page) || !ANSWERS.has(answer)) return json({ ok: false }, 400);

  if (!env.NEW_TO_YOU) {
    console.error("new-to-you: KV store NEW_TO_YOU not bound");
    return json({ ok: false }, 503);
  }

  const month = new Date().toISOString().slice(0, 7);
  const key = `${page} · ${answer} · ${month}`;
  const now = parseInt((await env.NEW_TO_YOU.get(key)) || "0", 10) || 0;
  await env.NEW_TO_YOU.put(key, String(now + 1));
  return json({ ok: true });
}

export async function onRequest({ request }) {
  if (request.method === "POST") return; // handled by onRequestPost
  return json({ ok: false }, 405);
}

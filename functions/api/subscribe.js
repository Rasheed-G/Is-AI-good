// Cloudflare Pages Function — POST /api/subscribe
// ---------------------------------------------------------------------------
// The email sign-up. The browser posts { email, website, page } (see site/frame.js). This
// function checks the address and adds it to our EmailOctopus list as "pending", so
// EmailOctopus sends its confirmation email (double opt-in). We store nothing ourselves:
// the address goes only to EmailOctopus, and only the address (no name, no page).
//
// --- Required environment (Pages project → Settings → Environment variables) ---
//   EMAILOCTOPUS_API_KEY   EmailOctopus API key (encrypted)
//   EMAILOCTOPUS_LIST_ID   The list people join
//
// Replies { ok: true } when the address is accepted, including when it's already on the list
// (so the form never reveals who has signed up). Anything else is { ok: false }.

const MAX_EMAIL = 254;
const MAX_BODY_BYTES = 4 * 1024;
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });

export async function onRequestPost({ request, env }) {
  let body;
  try {
    const raw = await request.text();
    if (raw.length > MAX_BODY_BYTES) return json({ ok: false, error: "too_large" }, 413);
    body = JSON.parse(raw || "{}");
  } catch {
    return json({ ok: false, error: "bad_json" }, 400);
  }

  // Honeypot: people never fill this hidden field. Pretend it worked so bots learn nothing.
  if (body.website) return json({ ok: true });

  const email = String(body.email || "").trim().toLowerCase();
  if (!email || email.length > MAX_EMAIL || !EMAIL_RE.test(email)) {
    return json({ ok: false, error: "bad_email" }, 400);
  }

  if (!env.EMAILOCTOPUS_API_KEY || !env.EMAILOCTOPUS_LIST_ID) {
    console.error("subscribe: EmailOctopus key or list id not set");
    return json({ ok: false, error: "not_configured" }, 503);
  }

  let res;
  try {
    res = await fetch(`https://api.emailoctopus.com/lists/${encodeURIComponent(env.EMAILOCTOPUS_LIST_ID)}/contacts`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.EMAILOCTOPUS_API_KEY}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ email_address: email, status: "pending" }),
    });
  } catch (err) {
    console.error("subscribe: EmailOctopus unreachable", err);
    return json({ ok: false, error: "upstream" }, 502);
  }

  // 201 = added (confirmation email on its way). 409 = already on the list: same reply, by design.
  if (res.status === 201 || res.status === 409) return json({ ok: true });

  console.error("subscribe: EmailOctopus said", res.status, (await res.text()).slice(0, 300));
  return json({ ok: false, error: "upstream" }, 502);
}

export async function onRequest({ request }) {
  if (request.method === "POST") return; // handled by onRequestPost
  return json({ ok: false, error: "method_not_allowed" }, 405);
}

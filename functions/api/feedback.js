// Cloudflare Pages Function — POST /api/feedback
// ---------------------------------------------------------------------------
// Holds the Airtable write token (which a static page must never carry) and
// writes visitor feedback into an Airtable "Feedback" table. The browser posts
// { type, comment, email, page, ts } (see site/app.js). This function validates,
// rate-limits, optionally verifies a Cloudflare Turnstile token, and writes.
//
// Cloudflare Pages picks up this file from the repo-root `functions/` dir and
// serves it at the route /api/feedback (matching the file path), regardless of
// the Pages build-output directory (site/). No build step needed.
//
// --- Required environment (Pages project → Settings → Environment variables) ---
//   AIRTABLE_TOKEN    Airtable PAT with data.records:write on the base below.
//                     Reuse the harvest token, or (better) a scoped write-only one.
//
// --- Optional environment (sensible defaults) ---
//   AIRTABLE_BASE     Airtable base id.        Default: appEiqYd3rnYwUNE7
//   FEEDBACK_TABLE    Table name (or tbl id).  Default: Feedback
//   TURNSTILE_SECRET  If set, a Turnstile token is REQUIRED and verified.
//                     (Also add the widget to the form + send `turnstileToken` —
//                      see docs/feedback-endpoint.md. Leave unset to skip.)
//
// --- Optional binding (real per-IP rate limiting) ---
//   FEEDBACK_RL       A KV namespace binding. If bound, each IP is limited to
//                     RL_MAX posts per RL_WINDOW_S seconds. If not bound, the
//                     honeypot + client throttle are the only brakes (fine to start).
//
// The Airtable "Feedback" table must exist with these fields (create in the UI):
//   Type (single line / single select), Comment (long text),
//   Email (single line), Page (URL or single line), User agent (single line),
//   plus a built-in Created-time field for arrival time (optional).
// Missing fields are tolerated: the write retries without any field Airtable
// rejects, so a minimal table (just Comment) still works.

const ALLOWED_TYPES = new Set(["Bug", "Suggestion", "Content issue", "Other"]);
const MAX_COMMENT = 4000;   // server ceiling (client caps at 1000)
const MAX_EMAIL = 200;
const MAX_PAGE = 500;
const MAX_BODY_BYTES = 16 * 1024;

const RL_MAX = 5;           // max posts per window per IP (when KV bound)
const RL_WINDOW_S = 3600;   // 1 hour

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });

export async function onRequestPost({ request, env }) {
  // --- read body (size-guarded) ---------------------------------------------
  let body;
  try {
    const raw = await request.text();
    if (raw.length > MAX_BODY_BYTES) return json({ error: "too_large" }, 413);
    body = JSON.parse(raw || "{}");
  } catch {
    return json({ error: "bad_json" }, 400);
  }

  // --- validate --------------------------------------------------------------
  const type = ALLOWED_TYPES.has(body.type) ? body.type : "Other";
  const comment = typeof body.comment === "string" ? body.comment.trim() : "";
  if (comment.length < 3) return json({ error: "comment_required" }, 400);
  if (comment.length > MAX_COMMENT) return json({ error: "comment_too_long" }, 400);

  let email = typeof body.email === "string" ? body.email.trim() : "";
  if (email) {
    if (email.length > MAX_EMAIL || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
      return json({ error: "bad_email" }, 400);
    }
  }

  const page = typeof body.page === "string" ? body.page.slice(0, MAX_PAGE) : "";
  const ua = (request.headers.get("user-agent") || "").slice(0, 300);
  const ip =
    request.headers.get("cf-connecting-ip") ||
    request.headers.get("x-forwarded-for") ||
    "";

  // --- optional Turnstile ----------------------------------------------------
  if (env.TURNSTILE_SECRET) {
    const token = typeof body.turnstileToken === "string" ? body.turnstileToken : "";
    if (!token) return json({ error: "captcha_required" }, 400);
    const ok = await verifyTurnstile(env.TURNSTILE_SECRET, token, ip);
    if (!ok) return json({ error: "captcha_failed" }, 403);
  }

  // --- optional per-IP rate limit (KV) --------------------------------------
  if (env.FEEDBACK_RL && ip) {
    const key = `rl:${ip}`;
    const count = parseInt((await env.FEEDBACK_RL.get(key)) || "0", 10);
    if (count >= RL_MAX) return json({ error: "rate_limited" }, 429);
    // best-effort increment; TTL resets the window
    await env.FEEDBACK_RL.put(key, String(count + 1), { expirationTtl: RL_WINDOW_S });
  }

  // --- write to Airtable -----------------------------------------------------
  const token = env.AIRTABLE_TOKEN;
  if (!token) return json({ error: "not_configured" }, 500);
  const base = env.AIRTABLE_BASE || "appEiqYd3rnYwUNE7";
  const table = env.FEEDBACK_TABLE || "Feedback";

  const fields = { Type: type, Comment: comment, Email: email, Page: page, "User agent": ua };
  const ok = await createRecord(token, base, table, fields);
  if (!ok) return json({ error: "store_failed" }, 502);

  return json({ ok: true });
}

// Non-POST → 405 (keeps the route honest).
export async function onRequest({ request }) {
  if (request.method === "POST") return; // handled by onRequestPost
  return json({ error: "method_not_allowed" }, 405);
}

async function verifyTurnstile(secret, token, ip) {
  try {
    const form = new FormData();
    form.append("secret", secret);
    form.append("response", token);
    if (ip) form.append("remoteip", ip);
    const res = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify", {
      method: "POST",
      body: form,
    });
    const data = await res.json();
    return !!data.success;
  } catch {
    return false;
  }
}

// Create one Airtable record. If Airtable rejects an unknown field (422), strip
// blank fields and retry once, then retry with just Comment — so a minimal table
// still accepts feedback instead of dropping it.
async function createRecord(token, base, table, fields) {
  const url = `https://api.airtable.com/v0/${base}/${encodeURIComponent(table)}`;
  const attempts = [
    fields,
    prune(fields),                 // drop empty-string fields
    { Comment: fields.Comment },   // last resort: minimal
  ];
  for (const f of attempts) {
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
        body: JSON.stringify({ records: [{ fields: f }], typecast: true }),
      });
      if (res.ok) return true;
      // 422 = unknown field / bad value → try a smaller field set. Other errors: stop.
      if (res.status !== 422) return false;
    } catch {
      return false;
    }
  }
  return false;
}

function prune(fields) {
  const out = {};
  for (const [k, v] of Object.entries(fields)) if (v !== "" && v != null) out[k] = v;
  return out;
}

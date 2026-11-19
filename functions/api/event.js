// Cloudflare Pages Function — POST /api/event
// ---------------------------------------------------------------------------
// Counts how pages are read and used: page views, how far people scroll, roughly how long they
// stay, and a few clicks. The browser sends { page, event, target } (see site/frame.js).
// Counts only: no cookies, no IP, nothing that identifies a person. Each event adds 1 to a daily
// total for that page.
//
// Stored in a D1 database, one row per day, page, event and target:
//   day         page           event         target          n
//   2026-11-19  /impact/work/  scroll-75                     14
//   2026-11-19  /              click-topic   /impact/work/    6
// D1 adds up safely when two visits land at the same moment (KV can drop one).
//
// --- Required binding (Pages project → Settings → Bindings → D1 database) ---
//   METRICS   The D1 database. If it isn't bound, events are dropped quietly; the site is unaffected.
// The table is created on first use.
//
// Only requests to isaigood.org are saved; previews answer as normal but save nothing.
// Anyone can post here, so the totals are a guide, not proof. Only the events listed below are
// accepted, and pages and targets must look like site paths.

const EVENTS = new Set([
  "view",
  "scroll-25", "scroll-50", "scroll-75", "scroll-100",
  "time-under-10s", "time-10-30s", "time-30s-2m", "time-2-5m", "time-over-5m",
  "click-topic",      // home page: a link to a topic page
  "click-theme",      // news page: a link from the gallery to a topic page
  "click-share",      // a story card's Share button
  "click-feedback",   // opened the feedback form
  "signup",           // sign-up form sent successfully (still needs the email confirmed)
  "not-found",        // page not found; page = the address people tried
]);
const PATH_RE = /^\/[A-Za-z0-9\-\/._~%]{0,150}$/;
const MAX_BODY_BYTES = 1024;

const SCHEMA = `CREATE TABLE IF NOT EXISTS counts (
  day TEXT NOT NULL, page TEXT NOT NULL, event TEXT NOT NULL, target TEXT NOT NULL DEFAULT '',
  n INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, page, event, target))`;
let ready = false;

const reply = (status) => new Response(null, { status, headers: { "cache-control": "no-store" } });

export async function onRequestPost({ request, env }) {
  // Live site only: preview deployments share the database, and their test clicks would mix in.
  if (!/(^|\.)isaigood\.org$/.test(new URL(request.url).hostname)) return reply(204);

  let body;
  try {
    const raw = await request.text();
    if (raw.length > MAX_BODY_BYTES) return reply(413);
    body = JSON.parse(raw || "{}");
  } catch {
    return reply(400);
  }

  const page = String(body.page || "");
  const event = String(body.event || "");
  const target = String(body.target || "");
  if (!PATH_RE.test(page) || !EVENTS.has(event)) return reply(400);
  if (target && !PATH_RE.test(target)) return reply(400);

  if (!env.METRICS) {
    console.error("event: D1 database METRICS not bound");
    return reply(503);
  }

  try {
    if (!ready) { await env.METRICS.prepare(SCHEMA).run(); ready = true; }
    const day = new Date().toISOString().slice(0, 10);
    await env.METRICS.prepare(
      `INSERT INTO counts (day, page, event, target, n) VALUES (?1, ?2, ?3, ?4, 1)
       ON CONFLICT (day, page, event, target) DO UPDATE SET n = n + 1`
    ).bind(day, page, event, target).run();
  } catch (err) {
    console.error("event: could not save", err);
    return reply(500);
  }
  return reply(204);
}

export async function onRequest({ request }) {
  if (request.method === "POST") return; // handled by onRequestPost
  return reply(405);
}

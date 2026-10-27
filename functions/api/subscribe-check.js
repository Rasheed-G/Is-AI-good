// Cloudflare Pages Function — GET /api/subscribe-check   (TEMPORARY: remove once sign-up is confirmed working)
// ---------------------------------------------------------------------------
// Asks EmailOctopus, with the same key and list id the sign-up uses, which list that is and what's on it.
// Shows no email addresses: only the list's name, its double opt-in setting, its counts, and the status
// and time of its five newest contacts.

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj, null, 2), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });

export async function onRequestGet({ env }) {
  if (!env.EMAILOCTOPUS_API_KEY || !env.EMAILOCTOPUS_LIST_ID) return json({ configured: false });
  const base = `https://api.emailoctopus.com/lists/${encodeURIComponent(env.EMAILOCTOPUS_LIST_ID)}`;
  const headers = { Authorization: `Bearer ${env.EMAILOCTOPUS_API_KEY}` };

  const listRes = await fetch(base, { headers });
  const list = await listRes.json().catch(() => ({}));
  const contactsRes = await fetch(`${base}/contacts?limit=5`, { headers });
  const contacts = await contactsRes.json().catch(() => ({}));

  return json({
    configured: true,
    list: {
      http: listRes.status,
      name: list.name,
      double_opt_in: list.double_opt_in,
      counts: list.counts,
      error: listRes.ok ? undefined : list.title || list.detail,
    },
    newest_contacts: {
      http: contactsRes.status,
      items: (contacts.data || []).map((c) => ({ status: c.status, created_at: c.created_at })),
      error: contactsRes.ok ? undefined : contacts.title || contacts.detail,
    },
  });
}

// Is AI Good? — story cards, shared by the news page (app.js) and theme pages (theme.js).
// card(story) builds one card exactly as the news page shows it. Videos and posts load only
// when the reader clicks (see onEmbedClick); attach onEmbedClick and onShareClick to the
// element holding the cards.

// Themes that have their own page: their cards link to it with this line (+ " →").
const THEME_PAGES = {
  "Jobs & livelihoods": { url: "/impact/work/", text: "Read more on how AI is changing jobs and livelihoods" },
};

function isSensitive(s) { return s.sensitivity === "Sensitive"; }


// ---- Embed detection -------------------------------------------------------

function detectMedia(s) {
  const url = s.mediaUrl || "";
  let m;
  if ((m = url.match(/(?:youtube\.com\/(?:watch\?v=|embed\/|shorts\/)|youtu\.be\/)([A-Za-z0-9_-]{11})/)))
    return { kind: "youtube", id: m[1] };
  if ((m = url.match(/instagram\.com\/(p|reel|tv)\/([A-Za-z0-9_-]+)/)))
    return { kind: "instagram", permalink: `https://www.instagram.com/${m[1]}/${m[2]}/` };
  if ((m = url.match(/tiktok\.com\/(?:.*\/video\/|embed\/v2\/|embed\/)(\d+)/)))
    return { kind: "tiktok", id: m[1], cite: url };
  if ((m = url.match(/c-span\.org\/video\/standalone\/\?c(\d+)/i)))
    return { kind: "cspan", id: m[1] };
  if ((m = url.match(/(?:twitter\.com|x\.com)\/([^\/]+)\/status\/(\d+)/)))
    return { kind: "twitter", url: `https://twitter.com/${m[1]}/status/${m[2]}` };
  if (/\.(jpe?g|png|webp|gif|avif)(\?|$)/i.test(url) || (s.format === "Image" && url))
    return { kind: "image", src: url };
  return null;
}

const PLATFORM = { youtube: "YouTube", cspan: "C-SPAN", instagram: "Instagram", tiktok: "TikTok", twitter: "X" };

// What a card shows before the reader clicks: a picture (YouTube thumbnail) or a cover, plus
// one button. The real embed is only built in embedHtml(), on click.
function mediaHtml(m) {
  if (m.kind === "image")
    return `<div class="card-media image"><img loading="lazy" src="${esc(m.src)}" alt=""></div>`;
  const name = PLATFORM[m.kind];
  const verb = m.kind === "youtube" || m.kind === "cspan" ? "Play" : "Open";
  const pic = m.kind === "youtube"
    ? `<img class="facade-img" loading="lazy" src="https://i.ytimg.com/vi/${esc(m.id)}/hqdefault.jpg" alt="">`
    : `<span class="ph-label" aria-hidden="true">${esc(name)}</span>`;
  return `<div class="card-media facade ${m.kind === "youtube" ? "has-img" : "placeholder"}" data-embed="${esc(JSON.stringify(m))}">
    ${pic}<button class="embed-load" type="button">${verb} on ${esc(name)}</button></div>`;
}

function embedHtml(m) {
  switch (m.kind) {
    case "youtube":
      return `<div class="card-media youtube"><iframe
        src="https://www.youtube-nocookie.com/embed/${esc(m.id)}?autoplay=1" title="Embedded video" allowfullscreen
        allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"></iframe></div>`;
    case "cspan":
      return `<div class="card-media youtube"><iframe
        src="https://www.c-span.org/video/standalone/?c${esc(m.id)}" title="Embedded C-SPAN video"
        allowfullscreen></iframe></div>`;
    case "instagram":
      return `<div class="card-media social"><blockquote class="instagram-media"
        data-instgrm-permalink="${esc(m.permalink)}" data-instgrm-version="14"
        style="margin:0;width:100%;max-width:540px"></blockquote></div>`;
    case "tiktok":
      return `<div class="card-media social"><blockquote class="tiktok-embed" cite="${esc(m.cite)}"
        data-video-id="${esc(m.id)}" style="margin:0;max-width:605px;min-width:280px"><section></section></blockquote></div>`;
    case "twitter":
      return `<div class="card-media social"><blockquote class="twitter-tweet" data-dnt="true">
        <a href="${esc(m.url)}"></a></blockquote></div>`;
  }
  return "";
}

// Click on "Play on …" / "Open on …": swap the cover for the real embed, then let the
// platform's script (if it has one) process it. Instagram/TikTok clips then hide the card's
// title + summary, since the clip is the content.
function onEmbedClick(e) {
  const btn = e.target.closest(".embed-load");
  if (!btn) return;
  const well = btn.closest(".facade");
  const m = JSON.parse(well.dataset.embed);
  const cardEl = well.closest(".card");
  well.outerHTML = embedHtml(m);
  if (m.kind === "instagram" || m.kind === "tiktok") cardEl.classList.add("social-video");
  enhanceEmbeds();
  const frame = cardEl.querySelector("iframe");
  if (frame) frame.focus();
}

// Cards whose source has no embeddable media (bot-blocked article, no image, self-post)
// still get a visual "cover" — the theme set in big brutalist type on a hatched panel —
// so the grid reads as intentional instead of a wall of bare text blocks.
function placeholderHtml(s) {
  const label = esc((s.theme || "Story").toUpperCase());
  return `<div class="card-media placeholder" aria-hidden="true"><span class="ph-label">${label}</span></div>`;
}

// Load each platform's official script once, then (re)process embeds after render.
const scriptLoaded = {};
function loadOnce(key, src, onReady) {
  if (scriptLoaded[key]) { onReady && onReady(); return; }
  const s = document.createElement("script");
  s.async = true; s.src = src;
  s.onload = () => { scriptLoaded[key] = true; onReady && onReady(); };
  document.body.appendChild(s);
}

function enhanceEmbeds(root = document) {
  if (root.querySelector(".twitter-tweet"))
    loadOnce("twitter", "https://platform.twitter.com/widgets.js",
      () => window.twttr && window.twttr.widgets && window.twttr.widgets.load(root));
  if (root.querySelector(".instagram-media"))
    loadOnce("instagram", "https://www.instagram.com/embed.js",
      () => window.instgrm && window.instgrm.Embeds.process());
  if (root.querySelector(".tiktok-embed")) {
    // TikTok's script processes blockquotes on load only, so re-inject to reprocess.
    const old = document.getElementById("tiktok-embed-script");
    if (old) old.remove();
    const s = document.createElement("script");
    s.id = "tiktok-embed-script"; s.async = true; s.src = "https://www.tiktok.com/embed.js";
    document.body.appendChild(s);
  }
}
// NB: there is deliberately NO dead-embed sweep. Instagram/TikTok cards always render and
// stay — we let the platform embed show whatever it shows (full post, or its own compact
// placeholder if the post is unavailable). An earlier sweep removed any social card whose
// embed iframe measured < 100px, but Instagram serves a ~98px stub to logged-out/bot-ish
// contexts, so the sweep was deleting live, valid stories on a failure that mostly only
// happens in test tooling. If a post is ever genuinely dead, clear that row in Airtable.

// ---- Card ------------------------------------------------------------------

// opts.readMore = false leaves out the "Read more" link (e.g. on the theme page it points to).
function card(s, opts = {}) {
  const el = document.createElement("article");
  const media = detectMedia(s);
  // Instagram / TikTok clips speak for themselves — the video IS the content, so an AI
  // summary of a video we can't verify adds nothing. On those cards we hide the title +
  // summary (kept in the DOM, visually-hidden, so search and screen readers still see them)
  // and let the embed stand alone. The card is never removed — if the embed can't render, the
  // platform shows its own placeholder, and the title/summary stay in the DOM for search/a11y.
  // (Clips only hide their text once loaded; see onEmbedClick.)
  el.className = "card" + (isSensitive(s) ? " sensitive" : "");

  el.innerHTML += media ? mediaHtml(media) : placeholderHtml(s);

  const tags = [`<span class="pill theme">${esc(s.theme || "")}</span>`];
  if (isSensitive(s)) tags.push(`<span class="pill sensitive">Sensitive topic</span>`);
  const dateTxt = s.date ? formatDate(s.date) : "";
  if (dateTxt) tags.push(`<span class="date">${esc(dateTxt)}</span>`);

  const shareUrl = s.sourceUrl || location.href;
  const foot =
    `<button class="share" type="button" data-url="${esc(shareUrl)}" data-title="${esc(s.title || "")}">⤴ Share</button>` +
    (s.sourceUrl
      ? `<a class="source" href="${esc(s.sourceUrl)}" target="_blank" rel="noopener noreferrer">View source →</a>`
      : "");

  const page = opts.readMore === false ? null : THEME_PAGES[s.theme];
  const readMore = page ? `<a class="read-more" href="${page.url}">${esc(page.text)} →</a>` : "";
  el.innerHTML += `
    <div class="card-body">
      <div class="card-tags">${tags.join("")}</div>
      <h2 class="card-title">${esc(s.title || "")}</h2>
      <p class="summary">${esc(s.summary || "")}</p>
      ${readMore}
      <div class="card-foot">${foot}</div>
    </div>`;
  return el;
}

async function onShareClick(e) {
  const btn = e.target.closest(".share");
  if (!btn) return;
  const url = btn.dataset.url, title = btn.dataset.title || "Is AI Good?";
  try {
    if (navigator.share) { await navigator.share({ title, text: title, url }); return; }
    await navigator.clipboard.writeText(url); flash(btn);
  } catch (_) {
    try { await navigator.clipboard.writeText(url); flash(btn); } catch (__) {}
  }
}
function flash(btn) {
  const original = btn.textContent;
  btn.textContent = "✓ Link copied"; btn.classList.add("copied");
  setTimeout(() => { btn.textContent = original; btn.classList.remove("copied"); }, 1500);
}

function formatDate(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return esc(iso);
  return d.toLocaleDateString("en-GB", { year: "numeric", month: "short", day: "numeric" });
}

function esc(str) {
  return String(str).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

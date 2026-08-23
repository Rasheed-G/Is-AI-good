// Is AI Good? — gallery. Loads stories.json (built from Airtable) and renders a
// searchable, theme-filterable grid, newest first. No build tools.
//
// Media embedding:
//   YouTube  -> plain iframe (reliable)
//   Image    -> <img>
//   Instagram / TikTok / X -> their OFFICIAL embed scripts (blockquote + JS),
//     because raw iframes to these platforms render blank or mis-sized.

let ALL = [];
let activeTheme = "All";
let query = "";
let showSensitive = false;

// Pagination: render the grid in batches instead of all at once, so performance
// (initial paint + the whole-grid rebuild on every search keystroke) stays flat as
// the story count grows. currentList = the full filtered list; shown = how many of
// it are in the DOM. "Load more" appends the next PAGE.
const PAGE = 30;
let currentList = [];
let shown = 0;

const $grid = document.getElementById("grid");
const $themes = document.getElementById("themes");
const $count = document.getElementById("count");
const $empty = document.getElementById("empty");
const $search = document.getElementById("search");
const $sens = document.getElementById("sensToggle");
const $filtersToggle = document.getElementById("filtersToggle");
const $filtersPanel = document.getElementById("filtersPanel");
const $themeToggle = document.getElementById("themeToggle");
const $themeCurrent = document.getElementById("themeCurrent");
const $loadMore = document.getElementById("loadMore");

init();

async function init() {
  try {
    const res = await fetch("stories.json", { cache: "no-store" });
    ALL = (await res.json()).stories || [];
  } catch (e) {
    $grid.innerHTML = "<p class='empty'>Could not load stories.</p>";
    return;
  }
  // Most recent first; undated stories sort to the end.
  ALL.sort((a, b) => dateVal(b.date) - dateVal(a.date));

  buildThemeChips();
  $search.addEventListener("input", debounce(() => {
    query = $search.value.toLowerCase().trim(); render();
  }, 200));
  $sens.addEventListener("change", () => { showSensitive = $sens.checked; render(); });

  // Mobile: search, the sensitive toggle, and the theme filters collapse behind
  // #filtersToggle; tapping it expands/hides the whole panel.
  $filtersToggle.addEventListener("click", () => {
    const open = $filtersPanel.classList.toggle("open");
    $filtersToggle.setAttribute("aria-expanded", open ? "true" : "false");
  });
  // Mobile: within the panel, the theme chips nest behind their own #themeToggle.
  $themeToggle.addEventListener("click", () => {
    const open = $themes.classList.toggle("open");
    $themeToggle.setAttribute("aria-expanded", open ? "true" : "false");
  });

  // Share (event-delegated) — system share sheet on mobile, clipboard on desktop.
  $grid.addEventListener("click", onShareClick);

  $loadMore.addEventListener("click", appendNext);

  render();
}

function dateVal(iso) { const t = Date.parse(iso || ""); return isNaN(t) ? -Infinity : t; }

function buildThemeChips() {
  const themes = ["All", ...Array.from(new Set(ALL.map(s => s.theme).filter(Boolean))).sort()];
  $themes.innerHTML = "";
  themes.forEach(t => {
    const b = document.createElement("button");
    b.className = "chip" + (t === activeTheme ? " active" : "");
    b.textContent = t;
    b.addEventListener("click", () => {
      activeTheme = t;
      document.querySelectorAll(".chip").forEach(c => c.classList.toggle("active", c.textContent === t));
      // Keep the theme sub-toggle's label in sync, then collapse the chip list (no-op on desktop).
      $themeCurrent.textContent = t;
      $themes.classList.remove("open");
      $themeToggle.setAttribute("aria-expanded", "false");
      render();
    });
    $themes.appendChild(b);
  });
}

// Theme + text match, ignoring the sensitive toggle (used for counting too).
function matchBase(s) {
  const themeOk = activeTheme === "All" || s.theme === activeTheme;
  const q = query;
  const searchOk = !q ||
    (s.title || "").toLowerCase().includes(q) ||
    (s.summary || "").toLowerCase().includes(q) ||
    (s.theme || "").toLowerCase().includes(q);
  return themeOk && searchOk;
}
function isSensitive(s) { return s.sensitivity === "Sensitive"; }

function render() {
  const base = ALL.filter(matchBase);
  currentList = base.filter(s => showSensitive || !isSensitive(s));

  // Reset to the first page whenever the filter/search/theme changes.
  $grid.innerHTML = "";
  shown = 0;
  appendNext();

  $empty.hidden = currentList.length > 0;

  lastHidden = showSensitive ? 0 : base.filter(isSensitive).length;
  setCount(currentList.length, lastHidden);
}

// Append the next PAGE cards of currentList to the grid (built in a fragment so the
// DOM is touched once), then show/hide the "Load more" button and process any embeds
// in the newly-added cards.
function appendNext() {
  const end = Math.min(shown + PAGE, currentList.length);
  const frag = document.createDocumentFragment();
  for (let i = shown; i < end; i++) frag.appendChild(card(currentList[i]));
  $grid.appendChild(frag);
  shown = end;

  $loadMore.hidden = shown >= currentList.length;

  enhanceEmbeds();
}

let lastHidden = 0;
function setCount(n, hidden) {
  const noun = n === 1 ? "story" : "stories";
  $count.textContent = hidden > 0 ? `${n} ${noun} · ${hidden} sensitive hidden` : `${n} ${noun}`;
}

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

function mediaHtml(m) {
  switch (m.kind) {
    case "youtube":
      return `<div class="card-media youtube"><iframe loading="lazy"
        src="https://www.youtube.com/embed/${m.id}" title="Embedded video" allowfullscreen
        allow="accelerometer; clipboard-write; encrypted-media; gyroscope; picture-in-picture"></iframe></div>`;
    case "image":
      return `<div class="card-media image"><img loading="lazy" src="${esc(m.src)}" alt=""></div>`;
    case "cspan":
      return `<div class="card-media youtube"><iframe loading="lazy"
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

function enhanceEmbeds() {
  if ($grid.querySelector(".twitter-tweet"))
    loadOnce("twitter", "https://platform.twitter.com/widgets.js",
      () => window.twttr && window.twttr.widgets && window.twttr.widgets.load($grid));
  if ($grid.querySelector(".instagram-media"))
    loadOnce("instagram", "https://www.instagram.com/embed.js",
      () => window.instgrm && window.instgrm.Embeds.process());
  if ($grid.querySelector(".tiktok-embed")) {
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

function card(s) {
  const el = document.createElement("article");
  const media = detectMedia(s);
  // Instagram / TikTok clips speak for themselves — the video IS the content, so an AI
  // summary of a video we can't verify adds nothing. On those cards we hide the title +
  // summary (kept in the DOM, visually-hidden, so search and screen readers still see them)
  // and let the embed stand alone. The card is never removed — if the embed can't render, the
  // platform shows its own placeholder, and the title/summary stay in the DOM for search/a11y.
  const socialVideo = !!media && (media.kind === "instagram" || media.kind === "tiktok");
  el.className = "card" + (isSensitive(s) ? " sensitive" : "") + (socialVideo ? " social-video" : "");

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

  const textCls = socialVideo ? " vh" : "";   // vh = visually hidden (kept for search/a11y)
  el.innerHTML += `
    <div class="card-body">
      <div class="card-tags">${tags.join("")}</div>
      <h2 class="card-title${textCls}">${esc(s.title || "")}</h2>
      <p class="summary${textCls}">${esc(s.summary || "")}</p>
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

function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

function formatDate(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return esc(iso);
  return d.toLocaleDateString("en-GB", { year: "numeric", month: "short", day: "numeric" });
}

function esc(str) {
  return String(str).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---- Feedback (floating button + modal) ------------------------------------
// The form is fully functional client-side. SENDING is inert until FEEDBACK_ENDPOINT is set
// to a server-side proxy URL — a static page must never carry an Airtable write token, so the
// POST target must be a small serverless function (Cloudflare Pages / Netlify / Vercel) that
// holds the token and does the real spam checks (Turnstile + per-IP rate limit). To go live:
// set FEEDBACK_ENDPOINT below and have that endpoint accept { type, comment, email, page, ts }.
const FEEDBACK_ENDPOINT = "/api/feedback";   // Cloudflare Pages Function — functions/api/feedback.js
const FB_MIN_INTERVAL_MS = 30000;    // client throttle: one send per 30s (a courtesy, not security)

(function initFeedback() {
  const openBtn = document.getElementById("fbOpen");
  const panel = document.getElementById("fbPanel");
  if (!openBtn || !panel) return;
  const closeBtn = document.getElementById("fbClose");
  const form = document.getElementById("fbForm");
  const note = document.getElementById("fbNote");
  const submit = document.getElementById("fbSubmit");
  const comment = document.getElementById("fbComment");
  let lastFocus = null;

  function open() {
    lastFocus = document.activeElement;
    panel.hidden = false;
    document.addEventListener("keydown", onKey);
    document.getElementById("fbType").focus();
  }
  function close() {
    panel.hidden = true;
    document.removeEventListener("keydown", onKey);
    setMsg("", "");
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }
  function onKey(e) { if (e.key === "Escape") close(); }
  function setMsg(text, cls) { note.textContent = text; note.className = "fb-note" + (cls ? " " + cls : ""); }

  openBtn.addEventListener("click", open);
  closeBtn.addEventListener("click", close);
  // Click the dimmed backdrop (not the card) to close.
  panel.addEventListener("click", e => { if (e.target === panel) close(); });

  form.addEventListener("submit", async e => {
    e.preventDefault();
    // Honeypot: real users never see this field; if it's filled, it's a bot — pretend success.
    if (form.website.value.trim()) { setMsg("Thanks!", "ok"); form.reset(); return; }
    const text = comment.value.trim();
    if (text.length < 3) { setMsg("Please add a little detail before sending.", "err"); comment.focus(); return; }
    const email = form.email.value.trim();
    if (email && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) { setMsg("That email doesn't look right — or leave it blank.", "err"); form.email.focus(); return; }

    const last = +localStorage.getItem("fbLast") || 0;
    if (Date.now() - last < FB_MIN_INTERVAL_MS) { setMsg("You just sent feedback — thank you! Give it a moment.", "err"); return; }

    const payload = { type: form.type.value, comment: text, email, page: location.href, ts: new Date().toISOString() };

    if (!FEEDBACK_ENDPOINT) {   // not wired yet — validate + acknowledge honestly, send nothing
      setMsg("Feedback isn't connected yet — the form works, but sending goes live once the site is hosted. Thanks for trying it!", "ok");
      return;
    }
    submit.disabled = true; setMsg("Sending…", "");
    try {
      const res = await fetch(FEEDBACK_ENDPOINT, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
      });
      if (!res.ok) throw new Error(res.status);
      localStorage.setItem("fbLast", String(Date.now()));
      setMsg("Thank you — your feedback was sent.", "ok");
      form.reset();
    } catch (_) {
      setMsg("Couldn't send just now — please try again in a moment.", "err");
    } finally {
      submit.disabled = false;
    }
  });
})();

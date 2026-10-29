// Is AI Good? — gallery. Loads stories.json (built from Airtable) and renders a
// searchable, theme-filterable grid, newest first. No build tools.
//
// Media embedding (videos and posts load only when the reader clicks, so no other
// company's player or script runs, or sets cookies, until they choose it):
//   YouTube  -> thumbnail + "Play on YouTube"; click swaps in a youtube-nocookie iframe
//   C-SPAN   -> cover + "Play on C-SPAN"; click swaps in its iframe
//   Image    -> <img>
//   Instagram / TikTok / X -> cover + "Open on …"; click swaps in the blockquote and loads that
//     platform's OFFICIAL embed script, because raw iframes to these platforms render blank or mis-sized.
//
// The page can open on chosen themes: /news/?theme=jobs-livelihoods (several: comma-separated).
// Theme names and their one-line summaries come from the rulebook, via /themes.json.

let ALL = [];
// The rulebook's themes: [{name, slug, summary}] (empty if themes.json can't load; the page still works).
let THEMES = [];
// Themes the reader has picked; empty = all themes. Several can be on at once.
const activeThemes = new Set();
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
    const res = await fetch("/stories.json", { cache: "no-store" });
    ALL = (await res.json()).stories || [];
  } catch (e) {
    $grid.innerHTML = "<p class='empty'>Could not load stories.</p>";
    return;
  }
  try {
    const res = await fetch("/themes.json", { cache: "no-store" });
    THEMES = (await res.json()).themes || [];
  } catch (e) { /* no summaries: the chips still work */ }
  // Most recent first; undated stories sort to the end.
  ALL.sort((a, b) => dateVal(b.date) - dateVal(a.date));

  themesFromUrl();
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
  $grid.addEventListener("click", onEmbedClick);

  $loadMore.addEventListener("click", appendNext);

  render();
}

function dateVal(iso) { const t = Date.parse(iso || ""); return isNaN(t) ? -Infinity : t; }

function buildThemeChips() {
  const themes = ["All", ...Array.from(new Set(ALL.map(s => s.theme).filter(Boolean))).sort()];
  $themes.innerHTML = "";
  themes.forEach(t => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "chip";
    b.textContent = t;
    b.addEventListener("click", () => {
      if (t === "All") activeThemes.clear();
      else if (activeThemes.has(t)) activeThemes.delete(t);
      else activeThemes.add(t);
      syncChips();
      themesToUrl();
      render();
    });
    $themes.appendChild(b);
  });
  syncChips();
}

// Theme names as they appear in an address: "Jobs & livelihoods" -> "jobs-livelihoods".
function slug(name) { return name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, ""); }

// Links to themes that were renamed or split (release 3, rulebook v1) open the new themes.
// A theme that left (Even the experts are worried) opens all themes.
const OLD_THEME_SLUGS = {
  "deepfakes-image-abuse": ["image-abuse"],
  "surveillance": ["surveillance-privacy"],
  "automated-decisions-welfare": ["automated-decision-making"],
  "ai-companions-mental-health": ["relationships-mental-health"],
  "breaches-system-failures": ["when-ai-fails", "hacking-ai-acting-autonomously"],
  "environmental-data-centre-impact": ["the-environment-data-centres"],
  "even-the-experts-are-worried": [],
};

function themesFromUrl() {
  const asked = (new URLSearchParams(location.search).get("theme") || "").split(",").filter(Boolean);
  if (!asked.length) return;
  const wanted = asked.flatMap(w => OLD_THEME_SLUGS[w] || [w]);
  const bySlug = {};
  ALL.forEach(s => { if (s.theme) bySlug[slug(s.theme)] = s.theme; });
  wanted.forEach(w => { if (bySlug[w]) activeThemes.add(bySlug[w]); });
  // Show the new names in the address bar, so a copied link uses them.
  if (asked.some(w => w in OLD_THEME_SLUGS)) themesToUrl();
}
function themesToUrl() {
  const params = new URLSearchParams(location.search);
  if (activeThemes.size) params.set("theme", [...activeThemes].map(slug).join(","));
  else params.delete("theme");
  const q = params.toString().replace(/%2C/g, ",");
  history.replaceState(null, "", location.pathname + (q ? "?" + q : "") + location.hash);
}

// Mark the picked chips ("All" when none are picked) and keep the phone sub-toggle's label in step.
// The chip list stays open on phones so several themes can be picked in a row.
function syncChips() {
  $themes.querySelectorAll(".chip").forEach(c => {
    const on = c.textContent === "All" ? activeThemes.size === 0 : activeThemes.has(c.textContent);
    c.classList.toggle("active", on);
    c.setAttribute("aria-pressed", on ? "true" : "false");
  });
  $themeCurrent.textContent = activeThemes.size === 0 ? "All"
    : activeThemes.size === 1 ? [...activeThemes][0]
    : `${activeThemes.size} themes`;
  showThemeSummary();
}

// One theme picked: show its summary from the rulebook above the stories.
function showThemeSummary() {
  const $s = document.getElementById("themeSummary");
  const t = activeThemes.size === 1 ? THEMES.find(x => x.name === [...activeThemes][0]) : null;
  $s.textContent = t ? t.summary : "";
  $s.hidden = !t;
}

// Theme + text match, ignoring the sensitive toggle (used for counting too).
function matchBase(s) {
  const themeOk = activeThemes.size === 0 || activeThemes.has(s.theme);
  const q = query;
  const searchOk = !q ||
    (s.title || "").toLowerCase().includes(q) ||
    (s.summary || "").toLowerCase().includes(q) ||
    (s.theme || "").toLowerCase().includes(q);
  return themeOk && searchOk;
}
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
}

let lastHidden = 0;
function setCount(n, hidden) {
  const noun = n === 1 ? "story" : "stories";
  $count.textContent = hidden > 0 ? `${n} ${noun} · ${hidden} sensitive hidden` : `${n} ${noun}`;
}

function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }


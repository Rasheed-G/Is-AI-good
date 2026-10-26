// Is AI Good? — theme pages (e.g. /impact/work/). Uses cards.js for the story cards.
//   * "What it looks like": the stories named in #themeStories[data-ids], drawn as news-page cards
//   * "Was this new to you?": one click, counted per page (round 7 wires /api/new-to-you)
//   * links to a closed section (#how-big) open it; "Copy link" copies the page address
(function () {
  // ---- Story cards ----
  const $stories = document.getElementById("themeStories");
  if ($stories) {
    const ids = ($stories.dataset.ids || "").split(",").filter(Boolean);
    fetch("/stories.json", { cache: "no-store" })
      .then((r) => r.json())
      .then((data) => {
        const byId = {};
        (data.stories || []).forEach((s) => { byId[s.id] = s; });
        ids.forEach((id) => { if (byId[id]) $stories.appendChild(card(byId[id])); });
      })
      .catch(() => {
        $stories.innerHTML = "<p class='empty'>Could not load stories. <a href='/news/'>See the news page</a>.</p>";
      });
    $stories.addEventListener("click", onShareClick);
    $stories.addEventListener("click", onEmbedClick);
  }

  // ---- Was this new to you? ----
  document.querySelectorAll(".new-to-you").forEach((box) => {
    const note = box.querySelector(".new-to-you-note");
    box.querySelectorAll("button[data-answer]").forEach((btn) => btn.addEventListener("click", () => {
      box.querySelectorAll("button[data-answer]").forEach((b) => {
        b.disabled = true;
        b.setAttribute("aria-pressed", b === btn ? "true" : "false");
      });
      note.textContent = "Thanks for telling us.";
      // Counts only: the page and the answer, no personal data. Fails quietly.
      fetch("/api/new-to-you", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ page: location.pathname, answer: btn.dataset.answer }),
      }).catch(() => {});
    }));
  });

  // ---- Links to a closed section open it ----
  function openTarget() {
    const el = location.hash && document.getElementById(decodeURIComponent(location.hash.slice(1)));
    const details = el && (el.tagName === "DETAILS" ? el : el.closest("details"));
    if (details) { details.open = true; el.scrollIntoView(); }
  }
  window.addEventListener("hashchange", openTarget);
  openTarget();

  // ---- Send to a colleague: copy link ----
  document.querySelectorAll("[data-copy-link]").forEach((btn) => btn.addEventListener("click", async () => {
    const note = btn.parentElement.querySelector(".copy-note");
    const url = location.origin + location.pathname;
    try { await navigator.clipboard.writeText(url); note.textContent = "Link copied."; }
    catch (_) { note.textContent = url; }
  }));
})();

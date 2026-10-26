// Is AI Good? — theme pages (e.g. /impact/work/). Uses cards.js for the story cards.
//   * "What it looks like": the stories named in #themeStories[data-ids], drawn as news-page cards
//   * "Was this new to you?": one click, counted per page (round 7 wires /api/new-to-you)
//   * links to a closed section open it
//   * section headings: the orange bar grows in as each one comes into view (not with reduced motion)
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
        ids.forEach((id) => { if (byId[id]) $stories.appendChild(card(byId[id], { readMore: false })); });
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

  // ---- Section headings: bar grows in when seen ----
  const heads = document.querySelectorAll(".sec-title");
  if ("IntersectionObserver" in window && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
    document.documentElement.classList.add("sec-anim");
    const io = new IntersectionObserver((entries) => entries.forEach((e) => {
      if (e.isIntersecting) { e.target.classList.add("seen"); io.unobserve(e.target); }
    }), { rootMargin: "0px 0px -15% 0px" });
    heads.forEach((h) => io.observe(h));
  }
})();

// Is AI Good? — theme pages (e.g. /impact/work/). Uses cards.js for the story cards.
//   * "What it looks like": the stories named in #themeStories[data-ids], drawn as news-page cards
//   * "Was this new to you?": one click, counted per page (round 7 wires /api/new-to-you)
//   * tabs for the go-deeper sections (one shown at a time; arrow keys move between tabs)
//   * links to a closed section or another tab open it
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

  // ---- Tabs ----
  // Without scripts every panel shows with its own heading; with them, one panel at a time.
  const tabs = Array.from(document.querySelectorAll('.tablist [role="tab"]'));
  function selectTab(tab, focus) {
    tabs.forEach((t) => {
      const on = t === tab;
      t.setAttribute("aria-selected", on ? "true" : "false");
      t.tabIndex = on ? 0 : -1;
      document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
    });
    if (focus) tab.focus();
  }
  if (tabs.length) {
    document.documentElement.classList.add("has-tabs");
    tabs.forEach((tab, i) => {
      tab.addEventListener("click", () => selectTab(tab, false));
      tab.addEventListener("keydown", (e) => {
        const n = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
        if (n === undefined) return;
        e.preventDefault();
        selectTab(tabs[(n + tabs.length) % tabs.length], true);
      });
    });
    selectTab(tabs.find((t) => t.getAttribute("aria-selected") === "true") || tabs[0], false);
  }

  // ---- Links to a closed section or another tab open it ----
  function openTarget() {
    const el = location.hash && document.getElementById(decodeURIComponent(location.hash.slice(1)));
    if (!el) return;
    const details = el.tagName === "DETAILS" ? el : el.closest("details");
    if (details) details.open = true;
    const panel = el.closest('[role="tabpanel"]');
    if (panel) selectTab(document.getElementById(panel.getAttribute("aria-labelledby")), false);
    if (details || panel) (panel && el === panel ? panel.closest(".tabs") : el).scrollIntoView();
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

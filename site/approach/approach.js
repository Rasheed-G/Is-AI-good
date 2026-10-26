// Our approach (/approach/): the journey map, links into closed sections, the side menu and the funnel.
// Without this file the page still works: map gates and source numbers are plain links.
(function () {
  const mapBtns = Array.from(document.querySelectorAll(".map-gate-btn"));
  let panel = null;

  // ---- Journey map: tap a gate and its block opens under it ----
  function closePanel() {
    if (!panel) return;
    panel.remove();
    panel = null;
    mapBtns.forEach((b) => b.setAttribute("aria-expanded", "false"));
  }

  function openGate(n, focus) {
    const btn = mapBtns.find((b) => b.dataset.gate === String(n));
    const block = document.getElementById("gate-" + n);
    if (!btn || !block) return;
    closePanel();
    panel = document.createElement("div");
    panel.className = "map-panel";
    panel.id = "mapPanel";
    panel.setAttribute("role", "region");
    const title = document.createElement("h3");
    title.tabIndex = -1;
    title.innerHTML = block.querySelector("summary h3").innerHTML;
    panel.setAttribute("aria-label", title.textContent);
    panel.appendChild(title);
    const sub = block.querySelector(".gate-sub");
    if (sub) panel.appendChild(sub.cloneNode(true));
    Array.from(block.querySelector(".gate-body").children).forEach((el) => panel.appendChild(el.cloneNode(true)));
    const actions = document.createElement("div");
    actions.className = "map-panel-actions";
    if (n < 8) {
      const next = document.createElement("button");
      next.type = "button";
      next.className = "next";
      next.textContent = "Next gate →";
      next.addEventListener("click", () => openGate(n + 1, true));
      actions.appendChild(next);
    }
    const close = document.createElement("button");
    close.type = "button";
    close.textContent = "Close";
    close.addEventListener("click", () => { closePanel(); btn.focus(); });
    actions.appendChild(close);
    panel.appendChild(actions);
    btn.closest("li").appendChild(panel);
    btn.setAttribute("aria-expanded", "true");
    btn.setAttribute("aria-controls", "mapPanel");
    if (focus) {
      title.focus({ preventScroll: true });
      btn.scrollIntoView({ block: "start", behavior: smooth() });
    }
  }

  const smooth = () => (matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth");

  mapBtns.forEach((btn) => btn.addEventListener("click", (e) => {
    e.preventDefault();
    if (btn.getAttribute("aria-expanded") === "true") closePanel();
    else openGate(Number(btn.dataset.gate), true);
  }));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && panel && panel.contains(document.activeElement)) {
      const btn = mapBtns.find((b) => b.getAttribute("aria-expanded") === "true");
      closePanel();
      if (btn) btn.focus();
    }
  });

  // ---- Links to a closed gate, section or source open it ----
  function openTarget() {
    const el = location.hash && document.getElementById(decodeURIComponent(location.hash.slice(1)));
    if (!el) return;
    const details = el.tagName === "DETAILS" ? el : el.closest("details:not(.on-page-box)");
    if (details) {
      details.open = true;
      el.scrollIntoView();
    }
  }
  window.addEventListener("hashchange", openTarget);
  openTarget();

  // ---- Side menu: marks where you are; on phones it folds away after a jump ----
  const navBox = document.querySelector(".on-page-box");
  const navLinks = Array.from(document.querySelectorAll(".on-page a"));
  const wide = matchMedia("(min-width: 1100px)");
  const syncNav = () => { navBox.open = wide.matches; };
  syncNav();
  wide.addEventListener("change", syncNav);
  navLinks.forEach((a) => a.addEventListener("click", () => { if (!wide.matches) navBox.open = false; }));

  const targets = navLinks.map((a) => document.getElementById(a.hash.slice(1))).filter(Boolean);
  function markCurrent() {
    let current = targets[0];
    for (const t of targets) if (t.getBoundingClientRect().top < 120) current = t;
    navLinks.forEach((a) => {
      if (a.hash.slice(1) === current.id) a.setAttribute("aria-current", "true");
      else a.removeAttribute("aria-current");
    });
  }

  // ---- Reading progress bar ----
  const bar = document.querySelector(".reading-progress span");
  const main = document.getElementById("main");
  function progress() {
    const r = main.getBoundingClientRect();
    const total = r.height - innerHeight;
    const done = total > 0 ? Math.min(1, Math.max(0, -r.top / total)) : 1;
    bar.style.width = (done * 100).toFixed(1) + "%";
  }

  let ticking = false;
  function onScroll() {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => { markCurrent(); progress(); ticking = false; });
  }
  addEventListener("scroll", onScroll, { passive: true });
  addEventListener("resize", onScroll);
  onScroll();

  // ---- Funnel: bars draw in when it comes into view ----
  const funnel = document.querySelector(".funnel");
  if (funnel) {
    if ("IntersectionObserver" in window) {
      const io = new IntersectionObserver((entries) => {
        if (entries.some((e) => e.isIntersecting)) { funnel.classList.add("is-drawn"); io.disconnect(); }
      }, { threshold: 0.4 });
      io.observe(funnel);
    } else funnel.classList.add("is-drawn");
  }
})();

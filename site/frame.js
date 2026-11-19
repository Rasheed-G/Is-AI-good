// Is AI Good? — the shared site frame, loaded on every page: the reading counter, the menu and the footer sign-up.
// Menu: groups open on click (not hover), close on Esc, a click outside, or when focus leaves.
// On phones the three-bar button opens the whole menu and groups fold open in place.
// ---- Reading counter: cookieless totals per page per day -> /api/event (functions/api/event.js) ----
// Counts a page view, how far people scroll (25, 50, 75, 100%), roughly how long the page stays open
// and visible (in bands), and a few clicks. Nothing identifies a person: no cookies, no IDs, no storage.
(function () {
  const page = location.pathname;
  function send(event, target) {
    const body = JSON.stringify(target ? { page, event, target } : { page, event });
    try {
      if (navigator.sendBeacon && navigator.sendBeacon("/api/event", body)) return;
    } catch (_) {}
    fetch("/api/event", { method: "POST", body, keepalive: true }).catch(() => {});
  }
  window.countEvent = send;

  if (/^Page not found/.test(document.title)) { send("not-found"); return; }
  send("view");

  // Scroll: each mark counted once per visit. A page that fits on one screen counts as 100%.
  const marks = [25, 50, 75, 100];
  let next = 0;
  function onScroll() {
    const doc = document.documentElement;
    const seen = ((window.scrollY + window.innerHeight) / Math.max(doc.scrollHeight, 1)) * 100;
    while (next < marks.length && seen >= marks[next] - 2) send("scroll-" + marks[next++]);
    if (next >= marks.length) window.removeEventListener("scroll", onScroll);
  }
  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("load", onScroll);

  // Time on page: only while the tab is visible; sent once, when the reader leaves or switches away.
  let shown = 0, since = document.visibilityState === "visible" ? Date.now() : 0, sent = false;
  function band(s) {
    return s < 10 ? "time-under-10s" : s < 30 ? "time-10-30s" : s < 120 ? "time-30s-2m" : s < 300 ? "time-2-5m" : "time-over-5m";
  }
  function leave() {
    if (since) { shown += Date.now() - since; since = 0; }
    if (!sent) { sent = true; send(band(shown / 1000)); }
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") leave();
    else if (!since) since = Date.now();
  });
  window.addEventListener("pagehide", leave);

  // Clicks: topic links on the home page, gallery to topic links, Share, opening feedback.
  document.addEventListener("click", (e) => {
    const el = e.target.closest("a, button");
    if (!el) return;
    if (el.matches(".share")) return send("click-share");
    if (el.matches("#fbOpen, [data-feedback]")) return send("click-feedback");
    const href = el.tagName === "A" ? el.getAttribute("href") || "" : "";
    if (!href.startsWith("/impact/") || el.closest("header, footer")) return;
    const target = href.split(/[?#]/)[0];
    if (page === "/") send("click-topic", target);
    else if (page.startsWith("/news/")) send("click-theme", target);
  }, true);
})();

(function () {
  const menuBtn = document.querySelector(".menu-btn");
  const groupBtns = Array.from(document.querySelectorAll(".nav-group-btn"));
  const isOpen = (b) => b && b.getAttribute("aria-expanded") === "true";
  const setOpen = (b, open) => b && b.setAttribute("aria-expanded", open ? "true" : "false");

  function closeGroups(except) {
    groupBtns.forEach((b) => { if (b !== except) setOpen(b, false); });
  }

  groupBtns.forEach((b) => b.addEventListener("click", () => {
    const open = !isOpen(b);
    closeGroups(b);
    setOpen(b, open);
  }));
  document.querySelectorAll(".nav-group").forEach((g) => g.addEventListener("focusout", (e) => {
    if (!g.contains(e.relatedTarget)) setOpen(g.querySelector(".nav-group-btn"), false);
  }));

  if (menuBtn) menuBtn.addEventListener("click", () => {
    const open = !isOpen(menuBtn);
    setOpen(menuBtn, open);
    if (!open) closeGroups();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const group = groupBtns.find(isOpen);
    if (group) { closeGroups(); group.focus(); }
    else if (isOpen(menuBtn)) { setOpen(menuBtn, false); menuBtn.focus(); }
  });
  document.addEventListener("click", (e) => {
    if (e.target.closest(".header-bar")) return;
    closeGroups();
    setOpen(menuBtn, false);
  });

  // ---- Sign-up: our own form -> /api/subscribe -> EmailOctopus (double opt-in) ----
  document.querySelectorAll("form.signup").forEach((form) => {
    const input = form.querySelector('input[type="email"]');
    const btn = form.querySelector('button[type="submit"]');
    const note = form.querySelector(".signup-note");
    const fail = "Sorry, that didn't work. Please try again.";

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!input.value.trim() || !input.checkValidity()) {
        note.textContent = "Please enter a valid email address.";
        input.focus();
        return;
      }
      btn.disabled = true;
      try {
        const res = await fetch(form.action, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email: input.value.trim(), website: form.website.value, page: location.pathname }),
        });
        const data = res.ok ? await res.json().catch(() => ({})) : {};
        if (data.ok) {
          form.reset();
          note.textContent = "Nearly done. Check your inbox and click the link to confirm.";
          if (window.countEvent) window.countEvent("signup");
        } else {
          note.textContent = fail;
        }
      } catch (err) {
        note.textContent = fail;
      }
      btn.disabled = false;
    });
  });
})();

// ---- Feedback form (modal) -------------------------------------------------
// Opens from the footer's "Have feedback? Tell us", any link marked data-feedback, and the news
// page's floating button. Links fall back to /contact/ if scripts are off. Sends to the
// Cloudflare Pages Function functions/api/feedback.js, which holds the Airtable token.
(function () {
  const FEEDBACK_ENDPOINT = "/api/feedback";
  const FB_MIN_INTERVAL_MS = 30000;    // client throttle: one send per 30s (a courtesy, not security)
  const panel = document.getElementById("fbPanel");
  if (!panel) return;
  const closeBtn = document.getElementById("fbClose");
  const form = document.getElementById("fbForm");
  const note = document.getElementById("fbNote");
  const submit = document.getElementById("fbSubmit");
  const comment = document.getElementById("fbComment");
  let lastFocus = null;

  function open(e) {
    if (e) e.preventDefault();
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

  document.querySelectorAll("#fbOpen, [data-feedback]").forEach(el => el.addEventListener("click", open));
  closeBtn.addEventListener("click", close);
  // Click the dimmed backdrop (not the card) to close.
  panel.addEventListener("click", e => { if (e.target === panel) close(); });

  form.addEventListener("submit", async e => {
    e.preventDefault();
    // Honeypot: real users never see this field; if it's filled, it's a bot, so pretend success.
    if (form.website.value.trim()) { setMsg("Thanks!", "ok"); form.reset(); return; }
    const text = comment.value.trim();
    if (text.length < 3) { setMsg("Please add a little detail before sending.", "err"); comment.focus(); return; }
    const email = form.email.value.trim();
    if (email && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) { setMsg("That email doesn't look right. Check it, or leave it blank.", "err"); form.email.focus(); return; }

    let last = 0;
    try { last = +localStorage.getItem("fbLast") || 0; } catch (_) {}
    if (Date.now() - last < FB_MIN_INTERVAL_MS) { setMsg("You just sent feedback, thank you! Give it a moment.", "err"); return; }

    const payload = { type: form.type.value, comment: text, email, page: location.href, ts: new Date().toISOString() };
    submit.disabled = true; setMsg("Sending…", "");
    try {
      const res = await fetch(FEEDBACK_ENDPOINT, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
      });
      if (!res.ok) throw new Error(res.status);
      try { localStorage.setItem("fbLast", String(Date.now())); } catch (_) {}
      setMsg("Thank you, your feedback was sent.", "ok");
      form.reset();
    } catch (_) {
      setMsg("Couldn't send just now. Please try again in a moment.", "err");
    } finally {
      submit.disabled = false;
    }
  });
})();

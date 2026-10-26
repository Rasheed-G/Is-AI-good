// Is AI Good? — the shared site frame, loaded on every page: the menu and the footer sign-up.
// Menu: groups open on click (not hover), close on Esc, a click outside, or when focus leaves.
// On phones the three-bar button opens the whole menu and groups fold open in place.
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

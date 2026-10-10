// Is AI Good? — site menu, shared by the landing page and /news/.
// Opens/closes the panel under the header. Closes on Esc, on a click outside, and after
// choosing an item; focus goes back to the Menu button.
(function () {
  const btn = document.getElementById("menuBtn");
  const panel = document.getElementById("menuPanel");
  if (!btn || !panel) return;

  function setOpen(open, refocus) {
    panel.hidden = !open;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    if (!open && refocus) btn.focus();
  }

  btn.addEventListener("click", () => setOpen(panel.hidden, false));
  panel.addEventListener("click", (e) => {
    if (e.target.closest("a")) setOpen(false, true);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !panel.hidden) setOpen(false, true);
  });
  document.addEventListener("click", (e) => {
    if (!panel.hidden && !e.target.closest(".menu-wrap")) setOpen(false, false);
  });
})();

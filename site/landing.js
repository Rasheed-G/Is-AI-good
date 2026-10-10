// Is AI Good? — landing page. Keeps the four step buttons and the sideways card track in sync.
// The buttons are real links (#step-…), so they still work with JavaScript off.
(function () {
  const track = document.getElementById("stepTrack");
  const buttons = Array.from(document.querySelectorAll(".step-btn"));
  if (!track || !buttons.length) return;

  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");

  function setActive(id) {
    buttons.forEach((b) => {
      const on = b.dataset.target === id;
      b.classList.toggle("active", on);
      if (on) b.setAttribute("aria-current", "true"); else b.removeAttribute("aria-current");
    });
  }

  // Click a button -> scroll the track (only the track, never the page) to that card.
  buttons.forEach((b) => {
    b.addEventListener("click", (e) => {
      const card = document.getElementById(b.dataset.target);
      if (!card) return;
      e.preventDefault();
      track.scrollTo({ left: card.offsetLeft - track.offsetLeft, behavior: reduce.matches ? "auto" : "smooth" });
      setActive(b.dataset.target);
    });
  });

  // Swipe/scroll -> highlight the button for the card that is mostly in view.
  if ("IntersectionObserver" in window) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((en) => { if (en.isIntersecting) setActive(en.target.id); });
    }, { root: track, threshold: 0.6 });
    track.querySelectorAll(".step-card").forEach((c) => io.observe(c));
  }
})();

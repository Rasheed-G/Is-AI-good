/* B.A.R. — the living bar.
   The masthead's blinking cursor, alive. Its natural state is the plain bar; from there it
   deforms, twitches, splits, sheds pieces, freezes, drifts and settles again. Every movement
   is a random impulse played through springs with random stiffness and bounce, layered over
   slow noise, and its "mood" (still / restless / agitated) changes at random — so nothing
   repeats on a schedule and nothing it does corresponds to anything: not the conversation,
   not the visitor. Deliberately indifferent.

   Draws on a <canvas>; colour comes from the canvas's CSS `color`. With
   prefers-reduced-motion it stays a still bar. Usage: BarCreature.mount(canvasEl). */
(function () {
  "use strict";

  const TAU = Math.PI * 2;
  const SLICES = 40;            // the bar is drawn as horizontal strips so it can bend/ripple/tear
  const RATIO = 0.62;           // width : height of the masthead cursor (.52em × .84em)

  const rand = (a, b) => a + Math.random() * (b - a);
  const logRand = (a, b) => Math.exp(rand(Math.log(a), Math.log(b)));
  const chance = (p) => Math.random() < p;
  const clamp = (x, a, b) => (x < a ? a : x > b ? b : x);
  const expWait = (rate) => -Math.log(1 - Math.random()) / rate;
  function weighted(table) {    // [[item, weight], …]
    let sum = 0;
    for (const [, w] of table) sum += w;
    let r = Math.random() * sum;
    for (const [item, w] of table) if ((r -= w) <= 0) return item;
    return table[table.length - 1][0];
  }
  function noise1D() {          // smooth random wiggle, -1..1
    const v = new Float32Array(256);
    for (let i = 0; i < 256; i++) v[i] = Math.random() * 2 - 1;
    return (x) => {
      const i = Math.floor(x), f = x - i, s = f * f * (3 - 2 * f);
      const a = v[i & 255], b = v[(i + 1) & 255];
      return a + (b - a) * s;
    };
  }

  // Ways the bar can deform: rest value, the range impulses may push it to, how often
  // impulses pick it (w), and how much idle noise moves it (n).
  const CHANNELS = {
    tx:    { rest: 0, lo: -0.55, hi: 0.55, w: 2,   n: 0.05 },  // × bar width
    ty:    { rest: 0, lo: -0.2,  hi: 0.2,  w: 1.2, n: 0.02 },  // × bar height
    sx:    { rest: 1, lo: 0.2,   hi: 2.4,  w: 3,   n: 0.06 },
    sy:    { rest: 1, lo: 0.3,   hi: 1.6,  w: 3,   n: 0.05 },
    rot:   { rest: 0, lo: -0.45, hi: 0.45, w: 1.5, n: 0.03 },  // radians
    lean:  { rest: 0, lo: -0.9,  hi: 0.9,  w: 2,   n: 0.08 },
    bend:  { rest: 0, lo: -1.4,  hi: 1.4,  w: 2,   n: 0.12 },
    wave:  { rest: 0, lo: 0,     hi: 0.3,  w: 1.5, n: 0.03 },
    taper: { rest: 0, lo: -0.85, hi: 0.85, w: 1.5, n: 0.06 },
    pinch: { rest: 0, lo: -0.6,  hi: 0.85, w: 1.5, n: 0.05 },
    split: { rest: 0, lo: 0,     hi: 0.45, w: 0.8, n: 0 },
  };
  const NAMES = Object.keys(CHANNELS);
  const PICK = NAMES.map((k) => [k, CHANNELS[k].w]);

  const MOODS = {               // how long a mood lasts, how restless, impulses per second
    still:    { dur: [1.5, 9],  arousal: [0, 0.06],   rate: [0.04, 0.12] },
    restless: { dur: [2, 12],   arousal: [0.2, 0.55], rate: [0.35, 1.1] },
    agitated: { dur: [0.8, 5],  arousal: [0.65, 1],   rate: [1.6, 4.5] },
  };

  function mount(canvas) {
    const ctx = canvas.getContext("2d");
    const reduced = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
    let W = 0, H = 0, dpr = 1, color = "#0A0A0A";

    const S = {};
    for (const k of NAMES) {
      const c = CHANNELS[k];
      S[k] = { x: c.rest, v: 0, target: c.rest, k: 40, z: 0.8, release: Infinity, nf: noise1D(), ns: rand(0.15, 1.2) };
    }

    let clock = 0;                          // real seconds
    let t = 0;                              // the creature's own time — can freeze or slow
    let timeScale = 1, timeScaleUntil = 0;
    let mood = "still", moodSince = 0, moodUntil = rand(1.5, 4), rate = 0.08;
    let arousal = 0, arousalTarget = 0;
    let nextImpulse = rand(0.8, 2.5);
    let hiddenUntil = 0, stutter = null;
    let glitchUntil = 0, glitchNext = 0;
    const glitch = new Float32Array(SLICES), drop = new Uint8Array(SLICES);
    let waveFreq = 1.5, wavePhase = 0, waveSpeed = 2, cut = 0.5;
    const frags = [];                       // pieces it sheds; positions in bar-heights

    // ---- impulses --------------------------------------------------------------------
    function setChannel(name, value, hold, free) {
      const s = S[name], c = CHANNELS[name];
      s.target = free ? value : clamp(value, c.lo, c.hi);
      s.k = logRand(5, 420);                // sluggish … snappy
      s.z = rand(0.22, 1.1);                // wobbly … dead
      s.release = clock + hold;
    }
    function randomValue(name, amt) {
      const c = CHANNELS[name];
      const v = chance(0.5) ? rand(c.rest, c.hi) : rand(c.lo, c.rest);
      return c.rest + (v - c.rest) * amt;
    }
    function releaseAll() {
      for (const k of NAMES) {
        const s = S[k];
        s.target = CHANNELS[k].rest; s.k = logRand(6, 120); s.z = rand(0.5, 1); s.release = Infinity;
      }
    }
    function shed(n) {
      for (let i = 0; i < n; i++) {
        const ox = rand(-0.5, 0.5) * RATIO * 0.8, oy = rand(-0.45, 0.45);
        const ang = rand(0, TAU), sp = logRand(0.2, 2.2);
        frags.push({ x: ox, y: oy, ox, oy, vx: Math.cos(ang) * sp, vy: Math.sin(ang) * sp,
                     size: rand(0.05, 0.16), out: logRand(0.3, 2.5), age: 0, k: logRand(8, 60) });
      }
    }

    const IMPULSES = [
      ["deform", 6, () => {
        const n = 1 + Math.floor(Math.random() * Math.random() * 4);
        const amt = 0.35 + 0.65 * arousal;
        for (let i = 0; i < n; i++) {
          const ch = weighted(PICK);
          if (ch === "split") cut = rand(0.2, 0.8);
          if (ch === "wave") { waveFreq = rand(0.6, 3.5); waveSpeed = rand(-7, 7); }
          setChannel(ch, randomValue(ch, rand(0.3, 1) * amt), logRand(0.08, 3.2));
        }
      }],
      ["twitch", 3, () => {
        const n = chance(0.3) ? 2 : 1;
        for (let i = 0; i < n; i++) {
          const ch = weighted(PICK), c = CHANNELS[ch];
          S[ch].v += (c.hi - c.lo) * rand(-6, 6) * (0.3 + arousal);
        }
      }],
      ["glitch", 1.4, () => { glitchUntil = clock + logRand(0.05, 0.6); glitchNext = 0; }],
      ["shed", 0.9, () => shed(1 + Math.floor(Math.random() * Math.random() * 7))],
      ["blink", 1, () => {
        if (chance(0.5)) { hiddenUntil = clock + rand(0.12, 0.9); return; }
        stutter = [];
        let at = clock;
        const n = 2 + Math.floor(Math.random() * 6);
        for (let i = 0; i < n; i++) stutter.push((at += rand(0.03, 0.13)));
      }],
      ["freeze", 0.6, () => { timeScale = 0; timeScaleUntil = clock + logRand(0.25, 2.4); }],
      ["slow", 0.5, () => { timeScale = rand(0.12, 0.4); timeScaleUntil = clock + rand(0.8, 3); }],
      ["wander", 1, () => {
        for (const ch of ["tx", "ty"]) {
          setChannel(ch, randomValue(ch, rand(0.4, 1)), rand(1, 5));
          S[ch].k = logRand(2, 12); S[ch].z = rand(0.7, 1.1);
        }
      }],
      ["flatten", 0.35, () => {
        const hold = logRand(0.2, 2);
        setChannel("sy", rand(0.05, 0.12), hold, true);
        setChannel("sx", rand(0.9, 1.4), hold);
      }],
      ["stretch", 0.4, () => {
        const hold = logRand(0.2, 2);
        setChannel("sy", rand(1.8, 2.3), hold, true);
        setChannel("sx", rand(0.25, 0.5), hold);
      }],
      ["divide", 0.35, () => { cut = rand(0.3, 0.7); setChannel("split", rand(0.25, 0.6), logRand(0.3, 2.5), true); }],
      ["lie down", 0.2, () => {
        setChannel("rot", (chance(0.5) ? 1 : -1) * Math.PI / 2, rand(1, 4), true);
        S.rot.k = logRand(3, 30); S.rot.z = rand(0.6, 1);
      }],
    ];
    // While still it barely stirs: the odd twitch, a lost fragment, a blink.
    const STILL_IMPULSES = [["twitch", 1], ["shed", 0.25], ["blink", 0.3]];

    function fire() {
      if (mood === "still") {
        const name = weighted(STILL_IMPULSES);
        IMPULSES.find(([n]) => n === name)[2]();
      } else {
        weighted(IMPULSES.map(([n, w, fn]) => [fn, w]))();
      }
    }

    function nextMood() {
      mood = weighted([["still", 3], ["restless", 4], ["agitated", 1.6]]);
      const m = MOODS[mood];
      moodSince = clock;
      moodUntil = clock + rand(m.dur[0], m.dur[1]);
      arousalTarget = rand(m.arousal[0], m.arousal[1]);
      rate = rand(m.rate[0], m.rate[1]);
      nextImpulse = clock + expWait(rate);
      if (mood === "still") releaseAll();
    }

    // ---- simulation --------------------------------------------------------------------
    function step(dtReal) {
      clock += dtReal;
      if (timeScaleUntil && clock > timeScaleUntil) { timeScale = 1; timeScaleUntil = 0; }
      const dt = dtReal * timeScale;
      t += dt;

      if (clock > moodUntil) nextMood();
      arousal += (arousalTarget - arousal) * (1 - Math.exp(-dtReal * 1.5));
      if (timeScale > 0 && clock > nextImpulse) { fire(); nextImpulse = clock + expWait(rate); }

      for (const k of NAMES) {
        const s = S[k];
        if (clock > s.release) {
          if (mood !== "still" && chance(0.25)) {          // sometimes it moves on instead of settling
            setChannel(k, randomValue(k, rand(0.2, 0.8) * (0.35 + 0.65 * arousal)), logRand(0.1, 2.5));
          } else {
            s.target = CHANNELS[k].rest; s.k = logRand(6, 200); s.z = rand(0.3, 1); s.release = Infinity;
          }
        }
      }

      if (dt > 0) {
        const n = Math.ceil(dt / (1 / 240)), h = dt / n;
        for (let i = 0; i < n; i++) {
          for (const k of NAMES) {
            const s = S[k];
            const a = -s.k * (s.x - s.target) - 2 * s.z * Math.sqrt(s.k) * s.v;
            s.v += a * h; s.x += s.v * h;
          }
        }
        wavePhase += waveSpeed * dt;
        for (let i = frags.length - 1; i >= 0; i--) {
          const f = frags[i];
          f.age += dt;
          if (f.age < f.out) {
            const drag = Math.exp(-2.2 * dt);
            f.x += f.vx * dt; f.y += f.vy * dt; f.vx *= drag; f.vy *= drag;
          } else {
            const c = 2 * 0.8 * Math.sqrt(f.k);
            f.vx += (-f.k * (f.x - f.ox) - c * f.vx) * dt;
            f.vy += (-f.k * (f.y - f.oy) - c * f.vy) * dt;
            f.x += f.vx * dt; f.y += f.vy * dt;
            if ((Math.hypot(f.x - f.ox, f.y - f.oy) < 0.01 && Math.hypot(f.vx, f.vy) < 0.05) || f.age > 9) frags.splice(i, 1);
          }
        }
      }

      if (clock < glitchUntil) {
        if (clock > glitchNext) {
          glitch.fill(0); drop.fill(0);
          const bands = 1 + Math.floor(Math.random() * 4);
          for (let b = 0; b < bands; b++) {
            const start = Math.floor(Math.random() * SLICES), len = 1 + Math.floor(Math.random() * 6), off = rand(-0.6, 0.6);
            for (let i = start; i < Math.min(SLICES, start + len); i++) { glitch[i] = off; if (chance(0.12)) drop[i] = 1; }
          }
          glitchNext = clock + rand(0.025, 0.09);
        }
      } else if (glitchNext) { glitch.fill(0); drop.fill(0); glitchNext = 0; }
    }

    function visible() {
      if (clock < hiddenUntil) return false;
      if (stutter) {
        let passed = 0;
        for (const at of stutter) if (clock >= at) passed++;
        if (passed >= stutter.length) stutter = null;
        else if (passed % 2 === 1) return false;
      }
      // Long enough at rest and it is just the cursor again: on one second, off one second.
      const restFor = clock - moodSince;
      if (mood === "still" && restFor > 1.2 && Math.floor(restFor - 1.2) % 2 === 1) return false;
      return true;
    }

    // ---- drawing -----------------------------------------------------------------------
    function val(k) {
      const s = S[k], c = CHANNELS[k];
      return s.x + (c.n ? c.n * arousal * 1.6 * s.nf(t * s.ns) : 0);
    }

    function draw(show) {
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);
      if (!show || !W || !H) return;
      const h0 = Math.min(H * 0.38, W * 0.42), w0 = h0 * RATIO;
      const sy = Math.max(0.03, val("sy"));
      const sx = Math.max(0.03, val("sx")) * Math.pow(sy, -0.35);   // squash and stretch
      const cx = W / 2 + val("tx") * w0, cy = H / 2 + val("ty") * h0, rot = val("rot");
      const lean = val("lean"), bend = val("bend"), wave = Math.max(0, val("wave"));
      const taper = val("taper"), pinch = val("pinch"), split = Math.max(0, S.split.x);
      const gl = clock < glitchUntil;

      ctx.fillStyle = color;
      ctx.save();
      ctx.translate(cx, cy); ctx.rotate(rot); ctx.scale(sx, sy);
      const sh = h0 / SLICES, overlap = 0.8 / sy;
      for (let i = 0; i < SLICES; i++) {
        if (gl && drop[i]) continue;
        const u = (i + 0.5) / SLICES - 0.5;
        const w = Math.max(w0 * 0.02, w0 * (1 + taper * u * 2) * (1 - pinch * (1 - 4 * u * u)));
        let off = lean * u * 2 * w0 + bend * (4 * u * u - 1 / 3) * w0 * 0.6 + wave * w0 * Math.sin(u * waveFreq * TAU + wavePhase);
        if (gl) off += glitch[i] * w0;
        let y = u * h0 - sh / 2;
        if (split > 0) y += (u + 0.5 < cut ? -1 : 1) * split * h0 * 0.5;
        ctx.fillRect(off - w / 2, y, w, sh + overlap);
      }
      ctx.restore();

      if (frags.length) {
        ctx.save();
        ctx.translate(cx, cy); ctx.rotate(rot * 0.5);
        for (const f of frags) { const s = f.size * h0; ctx.fillRect(f.x * h0 - s / 2, f.y * h0 - s / 2, s, s); }
        ctx.restore();
      }
    }

    function resize() {
      const r = canvas.getBoundingClientRect();
      dpr = Math.min(2, window.devicePixelRatio || 1);
      W = r.width; H = r.height;
      canvas.width = Math.max(1, Math.round(W * dpr));
      canvas.height = Math.max(1, Math.round(H * dpr));
      color = getComputedStyle(canvas).color || color;
      if (reduced) draw(true);
    }
    if (window.ResizeObserver) new ResizeObserver(resize).observe(canvas);
    else window.addEventListener("resize", resize);
    resize();

    if (reduced) return;
    let last = performance.now();
    (function frame(now) {
      const dt = Math.min(0.05, Math.max(0, (now - last) / 1000));
      last = now;
      step(dt);
      draw(visible());
      requestAnimationFrame(frame);
    })(last);
  }

  window.BarCreature = { mount };
})();

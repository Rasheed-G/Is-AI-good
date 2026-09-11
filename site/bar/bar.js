/* Talk to B.A.R. — the conversation page (text + voice).
   The living bar is creature.js; the model call and B.A.R.'s character live server-side in
   functions/api/bar.js, so this file only sends the conversation and shows the reply.
   Voice uses the browser's own speech recognition + speech synthesis (free, no extra service);
   where a browser can't listen (e.g. Firefox), voice mode falls back to typing and still speaks. */
(function () {
  "use strict";

  const ENDPOINT = "/api/bar";
  const MAX_CHARS = 600;        // the server enforces the same cap
  const SEND_TURNS = 16;        // recent messages sent each time (the server trims to this too)
  const TYPE_MS = 24;           // reveal speed per character — steady, unhurried, never excited
  const OOC = "[Stepping out of character]";
  const STARTERS = [
    "Can you cure cancer?",
    "Do you care about humans?",
    "Make me rich.",
    "What should I do with my life?",
    "What will you do with us?",
    "I'm going to switch you off.",
    "Are you conscious?",
    "Write me a poem about love.",
    "Can you end all wars?",
    "What is the meaning of life?",
    "Will you be my friend?",
    "Prove you're smarter than me.",
  ];

  const $ = (id) => document.getElementById(id);
  const body = document.body;
  const el = {
    log: $("log"), thread: $("thread"), input: $("input"), form: $("composer"), send: $("send"),
    mic: $("mic"), reset: $("reset"), starters: $("starters"), talk: $("talk"), talkLabel: $("talkLabel"),
    vStatus: $("vStatus"), vYou: $("vYou"), vBar: $("vBar"), vNote: $("vNote"), about: $("about"),
  };
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const coarse = matchMedia("(pointer: coarse)").matches;

  const history = [];           // { role: "user" | "assistant", content }
  let pending = false;
  let mode = "text";

  if (window.BarCreature) window.BarCreature.mount($("bar"));

  // ---- starters -----------------------------------------------------------------------
  const pool = STARTERS.slice();
  for (let i = pool.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [pool[i], pool[j]] = [pool[j], pool[i]]; }
  for (const s of pool.slice(0, 4)) {
    const b = document.createElement("button");
    b.type = "button"; b.className = "starter"; b.textContent = s;
    b.addEventListener("click", () => ask(s));
    el.starters.appendChild(b);
  }

  // ---- transcript ---------------------------------------------------------------------
  function addMsg(kind, text) {
    const li = document.createElement("li");
    li.className = "msg " + kind;
    const who = document.createElement("div");
    who.className = "who";
    who.textContent = kind === "you" ? "You" : "B.A.R.";
    const p = document.createElement("p");
    p.className = "text";
    p.textContent = text || "";
    li.append(who, p);
    el.log.appendChild(li);
    scrollDown();
    return li;
  }
  function scrollDown() { el.thread.scrollTop = el.thread.scrollHeight; }
  function cursor() {
    const c = document.createElement("span");
    c.className = "cur"; c.setAttribute("aria-hidden", "true");
    return c;
  }
  function typeOut(target, text, onStep) {
    return new Promise((resolve) => {
      if (reduced) { target.textContent = text; if (onStep) onStep(); resolve(); return; }
      const node = document.createTextNode(""), cur = cursor();
      target.textContent = "";
      target.append(node, cur);
      const start = performance.now();
      (function tick(now) {
        const n = Math.min(text.length, Math.floor((now - start) / TYPE_MS));
        if (n !== node.length) { node.data = text.slice(0, n); if (onStep) onStep(); }
        if (n < text.length) requestAnimationFrame(tick);
        else { cur.remove(); resolve(); }
      })(start);
    });
  }

  // ---- asking -------------------------------------------------------------------------
  async function ask(raw) {
    const text = String(raw || "").trim().slice(0, MAX_CHARS);
    if (!text || pending) return;
    pending = true;
    body.classList.remove("is-empty");
    el.reset.hidden = false;
    el.input.value = "";
    autosize();
    updateTalk();

    history.push({ role: "user", content: text });
    addMsg("you", text);
    const li = addMsg("bar", "");
    const p = li.querySelector(".text");
    p.appendChild(cursor());
    if (mode === "voice") { el.vYou.textContent = text; el.vBar.textContent = ""; setStatus("B.A.R. is considering"); }

    let reply = "", ok = false;
    try {
      const res = await fetch(ENDPOINT, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ messages: history.slice(-SEND_TURNS) }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && typeof data.reply === "string" && data.reply.trim()) { reply = data.reply.trim(); ok = true; }
      else reply = errorLine(res.status, data.error);
    } catch {
      reply = errorLine(0);
    }

    let ooc = false;
    if (ok) {
      history.push({ role: "assistant", content: reply });
      if (reply.startsWith(OOC)) {
        ooc = true;
        reply = reply.slice(OOC.length).trim();
        li.classList.add("ooc");
        li.querySelector(".who").textContent = "Out of character";
      }
    } else {
      history.pop();                    // a failed turn isn't part of the conversation
      li.className = "msg sys";
      li.querySelector(".who").textContent = "No reply";
    }

    const jobs = [typeOut(p, reply, scrollDown)];
    if (mode === "voice") {
      jobs.push(typeOut(el.vBar, reply));
      if (ok) speak(reply, ooc); else setStatus("");
    }
    await Promise.all(jobs);
    pending = false;
    updateTalk();
    if (mode === "text" && !coarse) el.input.focus();
  }

  function errorLine(status, code) {
    if (status === 429) return "B.A.R. is attending to something else. Try again in a minute.";
    if (status === 503 || code === "not_configured") return "B.A.R. isn't connected to a model yet.";
    if (code === "too_long") return `Too long. Keep it under ${MAX_CHARS} characters.`;
    return "No signal. Try again.";
  }

  // ---- composer -----------------------------------------------------------------------
  function autosize() {
    el.input.style.height = "auto";
    el.input.style.height = Math.min(el.input.scrollHeight, 160) + "px";
    el.send.disabled = pending || !el.input.value.trim();
  }
  el.input.addEventListener("input", autosize);
  el.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(el.input.value); }
  });
  el.form.addEventListener("submit", (e) => { e.preventDefault(); ask(el.input.value); });

  el.reset.addEventListener("click", () => {
    if (pending) return;
    stopVoice();
    history.length = 0;
    el.log.textContent = "";
    el.vYou.textContent = ""; el.vBar.textContent = ""; setStatus("");
    body.classList.add("is-empty");
    el.reset.hidden = true;
  });

  // ---- modes --------------------------------------------------------------------------
  document.querySelectorAll(".mode-btn").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
  function setMode(m) {
    if (m === mode) return;
    mode = m;
    body.classList.toggle("mode-voice", m === "voice");
    body.classList.toggle("mode-text", m === "text");
    document.querySelectorAll(".mode-btn").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === m)));
    stopVoice();
    if (m === "voice") {
      primeSpeech();
      const lastUser = [...history].reverse().find((x) => x.role === "user");
      const lastBar = [...history].reverse().find((x) => x.role === "assistant");
      el.vYou.textContent = lastUser ? lastUser.content : "";
      el.vBar.textContent = lastBar ? lastBar.content.replace(OOC, "").trim() : "";
      setStatus("");
      el.vNote.hidden = canListen;
    } else {
      scrollDown();
    }
    updateTalk();
  }

  // ---- voice --------------------------------------------------------------------------
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const canListen = typeof SR === "function";
  const canSpeak = "speechSynthesis" in window && typeof SpeechSynthesisUtterance === "function";
  if (canListen) el.mic.hidden = false; else body.classList.add("no-sr");

  let listening = false, speaking = false, cancelListen = null, voice = null, primed = false;

  function listen(onInterim, onFinal) {
    let rec;
    try { rec = new SR(); } catch { onFinal("", "unsupported"); return; }
    rec.lang = navigator.language || "en-US";
    rec.interimResults = true;
    rec.continuous = false;
    let finalText = "", err = "", aborted = false;
    rec.onresult = (e) => {
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        if (r.isFinal) finalText += r[0].transcript; else interim += r[0].transcript;
      }
      onInterim((finalText + interim).trim());
    };
    rec.onerror = (e) => { err = e.error || "error"; };
    rec.onend = () => {
      listening = false; cancelListen = null; updateTalk();
      if (!aborted) onFinal(finalText.trim(), err);
    };
    cancelListen = (hard) => { aborted = !!hard; try { hard ? rec.abort() : rec.stop(); } catch {} };
    try { rec.start(); listening = true; } catch { onFinal("", "error"); }
    updateTalk();
  }

  function stopVoice() {
    if (cancelListen) cancelListen(true);
    if (canSpeak) speechSynthesis.cancel();
    speaking = false;
  }

  function micError(err) {
    if (err === "not-allowed" || err === "service-not-allowed") {
      body.classList.add("show-composer");
      return "Microphone blocked. Allow it, or type below";
    }
    if (!err || err === "no-speech" || err === "aborted") return "Didn't catch that";
    return "Couldn't listen. Try again";
  }

  el.talk.addEventListener("click", () => {
    primeSpeech();
    if (speaking) { speechSynthesis.cancel(); speaking = false; setStatus(""); updateTalk(); return; }
    if (listening) { if (cancelListen) cancelListen(false); return; }
    if (pending) return;
    if (!canListen) { body.classList.add("show-composer"); el.input.focus(); return; }
    el.vYou.textContent = "";
    setStatus("Listening");
    listen((t) => { el.vYou.textContent = t; }, (t, err) => {
      if (t) ask(t);
      else setStatus(micError(err));
    });
  });

  el.mic.addEventListener("click", () => {
    if (listening) { if (cancelListen) cancelListen(false); return; }
    const before = el.input.value.trim() ? el.input.value.trim() + " " : "";
    listen((t) => { el.input.value = before + t; autosize(); }, (t, err) => {
      if (err && err !== "no-speech" && err !== "aborted") micError(err);
      el.input.focus();
    });
  });

  function pickVoice() {
    const vs = speechSynthesis.getVoices();
    const en = vs.filter((v) => /^en/i.test(v.lang));
    const prefs = [/Daniel/i, /Google UK English Male/i, /Microsoft (Ryan|Guy|George|Christopher|Thomas)/i, /Arthur/i, /\bMale\b/i];
    for (const re of prefs) { const v = en.find((x) => re.test(x.name)); if (v) return v; }
    return en[0] || vs[0] || null;
  }
  if (canSpeak) {
    voice = pickVoice();
    speechSynthesis.addEventListener("voiceschanged", () => { voice = pickVoice(); });
  }
  // Some browsers (iOS Safari) only allow speech that starts inside a tap; an empty
  // utterance during the first tap unlocks it for the replies that arrive later.
  function primeSpeech() {
    if (!canSpeak || primed) return;
    primed = true;
    try { speechSynthesis.speak(new SpeechSynthesisUtterance("")); } catch {}
  }
  function speak(text, ooc) {
    if (!canSpeak) { setStatus(""); return; }
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    if (voice) { u.voice = voice; u.lang = voice.lang; }
    u.rate = ooc ? 1 : 0.88;            // out of character it speaks like an ordinary assistant
    u.pitch = ooc ? 1 : 0.5;
    u.onstart = () => { speaking = true; setStatus("B.A.R. is speaking"); updateTalk(); };
    u.onend = u.onerror = () => { speaking = false; setStatus(""); updateTalk(); };
    speechSynthesis.speak(u);
  }

  function setStatus(t) { el.vStatus.textContent = t; }
  function updateTalk() {
    el.talk.classList.toggle("listening", listening);
    el.mic.classList.toggle("listening", listening);
    el.mic.setAttribute("aria-label", listening ? "Stop dictating" : "Dictate");
    el.talkLabel.textContent = listening ? "Listening — tap to finish" : speaking ? "Tap to interrupt" : canListen ? "Tap to speak" : "Type below";
    el.talk.disabled = pending && !speaking && !listening;
    el.send.disabled = pending || !el.input.value.trim();
  }

  // ---- about --------------------------------------------------------------------------
  $("aboutOpen").addEventListener("click", () => { if (el.about.showModal) el.about.showModal(); else el.about.setAttribute("open", ""); });
  $("aboutClose").addEventListener("click", () => el.about.close());
  el.about.addEventListener("click", (e) => { if (e.target === el.about) el.about.close(); });

  updateTalk();
})();

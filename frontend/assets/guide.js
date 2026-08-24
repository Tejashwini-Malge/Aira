/* Aira Guide — floating mascot with a one-line, per-screen explanation.
 * Auto-opens the first time a user lands on a given screen (once ever,
 * tracked in localStorage — pure client UI state, never server truth),
 * and stays available to reopen from the button afterwards. Skips
 * silently on any screen not listed in PAGES (marketing/auth pages).
 */
(function () {
  var PAGES = {
    welcome:     { title: "Your Classroom",        body: "This is home base. Work through the three periods in order — Persona, then Practice, then your Report Card. Come back here anytime to see where you left off." },
    onboarding:  { title: "Getting to know you",    body: "A few quick questions about your field, goal and experience. Takes about a minute and shapes everything Aira does next." },
    questions:   { title: "Building your Persona",  body: "Answer honestly — there's no wrong answer. Aira turns this into a private coach profile only it can see." },
    persona:     { title: "Profile ready",          body: "Your coach profile is built and stays private. Aira uses it behind the scenes to personalise every practice session." },
    reflection:  { title: "A couple more, in your words", body: "Open questions, no scoring pressure — just write what you'd actually say." },
    ai_quiz:     { title: "Mock Interview",         body: "Pick a role or topic. Aira generates fresh questions and scores you on accuracy and reasoning." },
    soft_skills: { title: "Soft Skills practice",   body: "Choose a track — self-intro, project talk, leadership and more. Scored on delivery, clarity and confidence, not just content." },
    framework:   { title: "Learn the framework",    body: "A quick, simple structure for this track. Read it once, then try it yourself in practice." },
    practice:    { title: "Speak it out loud",      body: "Answer when you're ready. Aira listens to how you actually said it, not just what you said." },
    report:      { title: "Your Report Card",       body: "Persona, quiz scores and speaking scores, all in one place. Come back after more practice to watch it improve." },
    profile:     { title: "Your account",           body: "Update your details, change your password, or review what Aira has on file for you." },
    analysis:    { title: "Session breakdown",      body: "A closer look at one result — what went well and what to focus on next time." },
    feedback:    { title: "Quick feedback",         body: "Thirty seconds, and it directly shapes what Aira builds next." }
  };

  function pageKey() {
    var name = (window.location.pathname.split("/").pop() || "").replace(/\.html$/i, "").toLowerCase();
    return name || "welcome";
  }

  function seenSet() {
    try { return JSON.parse(localStorage.getItem("aira_guide_seen") || "{}"); }
    catch (e) { return {}; }
  }
  function markSeen(key) {
    var s = seenSet();
    s[key] = true;
    try { localStorage.setItem("aira_guide_seen", JSON.stringify(s)); } catch (e) { /* private mode etc — fine to skip */ }
  }

  function init() {
    var key = pageKey();
    var content = PAGES[key];
    if (!content) return;

    var firstVisit = !seenSet()[key];

    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "aira-guide-btn" + (firstVisit ? " pulse" : "");
    btn.setAttribute("aria-label", "Open Aira guide");
    btn.innerHTML =
      '<svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">' +
      '<circle cx="16" cy="18" r="9" fill="none" stroke="#2F4B34" stroke-width="2"/>' +
      '<circle cx="12.5" cy="17" r="1.4" fill="#2F4B34"/>' +
      '<circle cx="19.5" cy="17" r="1.4" fill="#2F4B34"/>' +
      '<path d="M12 21.5 Q16 24 20 21.5" stroke="#2F4B34" stroke-width="1.8" fill="none" stroke-linecap="round"/>' +
      '<path d="M7 12 L16 7 L25 12" stroke="#C9973B" stroke-width="2" fill="none" stroke-linejoin="round"/>' +
      '<rect x="15" y="7" width="2" height="6" fill="#C9973B"/>' +
      "</svg>";

    var pop = document.createElement("div");
    pop.className = "aira-guide-pop";
    pop.innerHTML =
      '<div class="aira-guide-eyebrow">Aira guide</div>' +
      '<div class="aira-guide-title">' + content.title + "</div>" +
      '<div class="aira-guide-body">' + content.body + "</div>" +
      '<button type="button" class="aira-guide-close">Got it</button>';

    document.body.appendChild(btn);
    document.body.appendChild(pop);

    function open() { pop.classList.add("open"); btn.classList.remove("pulse"); markSeen(key); }
    function close() { pop.classList.remove("open"); }

    btn.addEventListener("click", function () {
      pop.classList.contains("open") ? close() : open();
    });
    pop.querySelector(".aira-guide-close").addEventListener("click", close);
    document.addEventListener("click", function (e) {
      if (!pop.contains(e.target) && !btn.contains(e.target)) close();
    });

    if (firstVisit) setTimeout(open, 700);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

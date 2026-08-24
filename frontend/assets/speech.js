/* Aira's voice — speechSynthesis output, paired with dictation.js's input.
 *
 * WHY THE MIC HANDLE IS A PARAMETER AND NOT THE CALLER'S PROBLEM
 *
 * If the browser is speaking a question out loud while SpeechRecognition is
 * listening, the recogniser hears Aira and transcribes the question into the
 * student's answer. Laptops with echo cancellation sometimes survive it; a phone
 * on speaker does not. The damage is the quiet kind — nothing crashes, the answer
 * just arrives at the evaluator with the question glued to the front of it and
 * comes back with a score that looks real. That is the same shape of failure as
 * the 37,719-character runaway on speaking_sessions.id 11.
 *
 * So the ONLY safe order is: stop the mic, speak, then resume. Leaving that
 * sequence to each call site means one of them eventually forgets, so say() owns
 * it — pass the mic handle in and the lock cannot be skipped.
 *
 * Support note: speechSynthesis works in Chrome, Edge, Firefox AND iOS Safari —
 * strictly wider than SpeechRecognition. Users who cannot speak their answer can
 * still be asked out loud. The question text is always on screen regardless; the
 * voice is additive and never a requirement.
 */
(function (global) {
  var synth = global.speechSynthesis;
  var supported = !!(synth && global.SpeechSynthesisUtterance);
  var MUTE_KEY = 'aira_voice_muted';

  // Indian English first — these are students preparing for interviews they will
  // sit in India. Then the other common English voices, then whatever exists.
  var PREFER = ['en-IN', 'en-GB', 'en-US', 'en'];

  var voice = null;
  var speaking = false;
  var current = null;

  function pickVoice() {
    if (!supported) return null;
    var voices = synth.getVoices() || [];
    if (!voices.length) return null;            // not loaded yet; voiceschanged retries
    for (var i = 0; i < PREFER.length; i++) {
      for (var j = 0; j < voices.length; j++) {
        var lang = (voices[j].lang || '').replace('_', '-');
        if (lang.toLowerCase().indexOf(PREFER[i].toLowerCase()) === 0) return voices[j];
      }
    }
    return voices[0];
  }

  if (supported) {
    voice = pickVoice();
    // getVoices() is empty on first call in Chrome — the list arrives later.
    if (synth.addEventListener) synth.addEventListener('voiceschanged', function () { voice = pickVoice(); });
    else synth.onvoiceschanged = function () { voice = pickVoice(); };
    // Never let a question keep talking into the next page.
    global.addEventListener('beforeunload', function () { try { synth.cancel(); } catch (e) {} });
  }

  function isMuted() {
    try { return global.localStorage.getItem(MUTE_KEY) === '1'; } catch (e) { return false; }
  }

  function setMuted(on) {
    try { global.localStorage.setItem(MUTE_KEY, on ? '1' : '0'); } catch (e) {}
    if (on) cancel();
  }

  function cancel() {
    current = null;
    speaking = false;
    if (supported) { try { synth.cancel(); } catch (e) {} }
  }

  /* say(text, { mic, onStart, onEnd })
   *
   * `mic` is optional and shaped { stop(), start(), isActive() }. It is stopped
   * before the first sound and resumed afterwards ONLY if it was already active —
   * a student who was typing stays typing, and nobody's mic switches itself on
   * because Aira finished a sentence.
   *
   * Always calls onEnd exactly once, including when muted, unsupported or failed,
   * so callers can drive UI state from it without a separate success path.
   */
  function say(text, opts) {
    opts = opts || {};
    var mic = opts.mic || null;
    var onStart = opts.onStart || function () {};
    var onEnd = opts.onEnd || function () {};
    var done = false;
    function finish() {
      if (done) return;
      done = true;
      speaking = false;
      current = null;
      onEnd();
    }

    // Was the student mid-dictation? Decided BEFORE we touch anything.
    var resume = !!(mic && mic.isActive && mic.isActive());

    if (!supported || isMuted() || !text || !String(text).trim()) {
      // Nothing was spoken, so the mic was never disturbed — leave it exactly as is.
      finish();
      return;
    }

    // Hard-stop first, unconditionally. Everything below can fail; this must not.
    if (mic && mic.stop) { try { mic.stop(); } catch (e) {} }

    try { synth.cancel(); } catch (e) {}

    var u = new global.SpeechSynthesisUtterance(String(text));
    if (voice) { u.voice = voice; u.lang = voice.lang; }
    u.rate = 0.98;
    u.pitch = 1;
    u.onstart = function () { speaking = true; onStart(); };
    // Resume only if this utterance is still the current one. cancel() clears
    // `current`, so a question cut short by Submit, Skip or a mute does NOT switch
    // the mic back on underneath whatever the user just moved to.
    function settle() {
      if (current === u && resume && mic && mic.start) { try { mic.start(); } catch (e) {} }
      finish();
    }
    u.onend = settle;
    // Blocked (no user gesture yet on iOS), interrupted, or no voice available.
    // The question is on screen either way — degrade silently, never block.
    u.onerror = settle;

    current = u;
    // cancel() immediately followed by speak() is unreliable in Chrome; let the
    // queue drain a tick first.
    global.setTimeout(function () {
      if (current !== u) return;   // superseded while we waited
      try { synth.speak(u); } catch (e) { finish(); }
    }, 0);
  }

  global.Speech = {
    supported: supported,
    say: say,
    cancel: cancel,
    isMuted: isMuted,
    setMuted: setMuted,
    isSpeaking: function () { return speaking; },
  };
})(window);

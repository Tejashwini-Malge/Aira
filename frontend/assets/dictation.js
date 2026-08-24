/* Shared dictation for Aira's spoken answers.
 *
 * Ported from practice.html, which learned these rules the hard way. The
 * production bug on speaking_sessions.id 11: the mic re-delivered each phrase one
 * word longer every time, so a ~40-word answer reached the server as 37,719
 * characters. That blew the evaluation prompt's context window, the model returned
 * nothing, and the student silently got 5/5/5/5 fallback scores as though they had
 * been graded. Two rules prevent it, and both are load-bearing:
 *
 *   1. `baseText` is the ONLY record of finalised speech, and is never re-read from
 *      the textarea. The textarea holds base + interim, so snapshotting it on
 *      restart promotes un-finalised interim into the base — and mobile Chrome
 *      restarts constantly, because it ignores `continuous`. Pending interim is
 *      dropped on restart instead: losing a trailing part-word beats duplicating
 *      the whole answer.
 *   2. appendSpeech() strips repeated trailing words, as a second line of defence
 *      in case the engine replays audio it already gave us.
 *
 * SpeechRecognition does not exist in Firefox and is unreliable on iOS Safari, so
 * every caller must keep its textarea usable on its own. `supported` is exposed so
 * the caller can adjust its copy — never to gate the question behind a mic.
 */
(function (global) {
  var SR = global.SpeechRecognition || global.webkitSpeechRecognition;
  var DEFAULT_MAX_CHARS = 3000;

  // Append `add` to `base`, skipping the longest run of trailing words in `base`
  // that `add` repeats. "the interest for" + "for admission" -> "the interest for admission".
  function appendSpeech(base, add) {
    var b = base.trim().split(/\s+/).filter(Boolean);
    var a = add.trim().split(/\s+/).filter(Boolean);
    if (!a.length) return base;
    var max = Math.min(b.length, a.length, 30);
    var overlap = 0;
    for (var k = max; k > 0; k--) {
      if (b.slice(b.length - k).join(' ').toLowerCase() === a.slice(0, k).join(' ').toLowerCase()) { overlap = k; break; }
    }
    return b.concat(a.slice(overlap)).join(' ') + ' ';
  }

  /* attach({ textarea, micBtn, statusEl, maxChars, idlePlaceholder,
   *          livePlaceholder, onChange })
   * Returns { supported, stop }. Safe to call when SR is missing: it hides the mic
   * and leaves the textarea completely untouched.
   */
  function attach(opts) {
    var ta = opts.textarea;
    var micBtn = opts.micBtn;
    var statusEl = opts.statusEl || null;
    var maxChars = opts.maxChars || DEFAULT_MAX_CHARS;
    var idlePlaceholder = opts.idlePlaceholder || 'Type here, or tap the mic and just talk.';
    var livePlaceholder = opts.livePlaceholder || "Listening — say it how you'd say it out loud.";
    var onChange = opts.onChange || function () {};

    if (!SR || !ta || !micBtn) {
      if (micBtn) micBtn.style.display = 'none';
      if (statusEl) statusEl.style.display = 'none';
      return { supported: false, stop: function () {} };
    }

    var recog = new SR();
    var isListening = false, wantListening = false, baseText = '';
    recog.continuous = true;
    recog.interimResults = true;
    recog.lang = 'en-US';

    function setStatus(txt) { if (statusEl) statusEl.textContent = txt; }

    function stop() {
      wantListening = false;
      try { recog.stop(); } catch (e) {}
      isListening = false;
      micBtn.classList.remove('listening');
      ta.placeholder = idlePlaceholder;
      setStatus('');
    }

    recog.onstart = function () {
      isListening = true;
      micBtn.classList.add('listening');
      ta.placeholder = livePlaceholder;
      setStatus('Listening…');
    };

    recog.onend = function () {
      isListening = false;
      if (wantListening) {
        // Silence timeout, not a user stop — resume from the finalised text only.
        // Do NOT re-snapshot ta.value here; that is what caused the runaway growth.
        ta.value = baseText.trim();
        try { recog.start(); return; } catch (e) { wantListening = false; }
      }
      micBtn.classList.remove('listening');
      ta.placeholder = idlePlaceholder;
      setStatus('');
    };

    recog.onresult = function (e) {
      var fin = '', int = '';
      for (var i = e.resultIndex; i < e.results.length; i++) {
        if (e.results[i].isFinal) fin += e.results[i][0].transcript + ' ';
        else int += e.results[i][0].transcript;
      }
      if (fin) baseText = appendSpeech(baseText, fin);
      if (baseText.length > maxChars) baseText = baseText.slice(0, maxChars);
      ta.value = (baseText + int).trim().slice(0, maxChars);
      onChange();
    };

    recog.onerror = function (e) {
      if (e.error === 'no-speech' || e.error === 'aborted') return; // harmless; onend handles restart
      stop();
      setStatus(e.error === 'not-allowed' ? 'Mic blocked — typing works just as well.' : 'Mic error — you can type instead.');
    };

    micBtn.addEventListener('click', function () {
      if (isListening || wantListening) { stop(); return; }
      wantListening = true;
      baseText = ta.value.trim() ? ta.value.trim() + ' ' : '';
      try { recog.start(); } catch (e) { wantListening = false; }
    });

    ta.placeholder = idlePlaceholder;
    return { supported: true, stop: stop };
  }

  global.Dictation = { supported: !!SR, appendSpeech: appendSpeech, attach: attach };
})(window);

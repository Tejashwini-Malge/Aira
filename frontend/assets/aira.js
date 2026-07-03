/* Shared Aira frontend helpers.
 *
 * One rule everywhere: the SERVER is the source of truth for auth and progress.
 * Pages must not mirror server state into localStorage — that mirror is exactly
 * how stale "logged in" / "persona done" bugs happened. localStorage is fine for
 * purely client-side UI state only.
 */
window.Aira = (function () {
  // fetch wrapper: same-origin cookies, JSON in/out, and a single place that
  // turns a 401 into the login redirect instead of every page swallowing it.
  async function api(path, opts) {
    opts = opts || {};
    const init = {
      method: opts.method || (opts.body ? "POST" : "GET"),
      credentials: "include",
      headers: {},
    };
    if (opts.body instanceof FormData) {
      init.body = opts.body; // browser sets the multipart boundary itself
    } else if (opts.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    const res = await fetch(path, init);
    if (res.status === 401 && !opts.allow401) {
      window.location.replace("login.html");
      // Give the redirect time to happen instead of letting callers run on.
      return new Promise(() => {});
    }
    let data = null;
    try { data = await res.json(); } catch (e) { /* non-JSON error page */ }
    if (!res.ok) {
      const err = new Error((data && (data.message || data.error)) || ("Request failed (" + res.status + ")"));
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  // Gate a page: resolves with the logged-in user, or redirects to login.
  async function requireAuth() {
    const data = await api("/me");
    return data.user;
  }

  // One /me/report call answers every "how far along are they" question the
  // pages used to answer from localStorage flags.
  async function progress() {
    const r = await api("/me/report");
    return {
      user: r.user,
      onboardingComplete: !!(r.user && r.user.onboarding_complete),
      personaReady: !!r.persona,
      quizzes: r.quizzes || [],
      speaking: r.speaking || [],
      speakingAverages: r.speaking_averages || null,
      recurringWeakAreas: r.recurring_weak_areas || [],
      quizHistory: r.quiz_history || [],
      speakingHistory: r.speaking_history || [],
      raw: r,
    };
  }

  // Old-school school bell, synthesized (no audio file, works offline).
  // Two-tone brass ring with a slow decay, like a wall-mounted classroom bell.
  function ringBell() {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      const ctx = new Ctx();
      const now = ctx.currentTime;
      [1600, 2000].forEach(function (freq, i) {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = "triangle";
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(0, now);
        gain.gain.linearRampToValueAtTime(0.18, now + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + 1.6);
        osc.connect(gain).connect(ctx.destination);
        osc.start(now + i * 0.12);
        osc.stop(now + 1.8);
      });
      setTimeout(function () { ctx.close(); }, 2200);
    } catch (e) { /* audio unsupported — silently skip */ }
  }

  return { api: api, requireAuth: requireAuth, progress: progress, ringBell: ringBell };
})();

"""Single Groq chat client shared by the persona, quiz and resume modules.

One place for the API key, endpoint, model name and the request/parse dance every
caller used to duplicate. Error POLICY stays with each caller: this module raises
GroqError on any failure and the callers translate that into their own semantics
(session_controller -> PersonaGenerationError, ai_quiz -> None fallback,
resume_agent -> ResumeError).
"""
import os
import re
import json
import requests
from pathlib import Path
from dotenv import load_dotenv

# Load .env next to this file regardless of the launch working directory.
load_dotenv(Path(__file__).resolve().parent / ".env")

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL_NAME = "openai/gpt-oss-120b"
# Groq's free-tier daily token quota is tracked PER MODEL, so every model added
# here brings its own separate allowance. Two tiers, because the work splits in
# two: judgement calls (persona, evaluation) want the big models, extraction calls
# (resume parse, question generation) do not and only ever spent the big models'
# quota by accident.
#
# Free-tier tokens/day at the time of writing: 120b 200k, 70b 100k,
# 8b-instant 500k, 20b 200k.
QUALITY_MODELS = [MODEL_NAME, "llama-3.3-70b-versatile"]
# gpt-oss-20b leads the cheap tier ahead of 8b-instant despite the smaller pool:
# 8b's 500k/day is the deepest reserve we have and is the only thing left once
# everything else is dry, so it is worth keeping in hand. 20b is also same-family
# as the primary, so it honours reasoning_effort (see _call_model) and behaves
# predictably under the low max_tokens caps our callers use.
CHEAP_MODELS = ["openai/gpt-oss-20b", "llama-3.1-8b-instant"]
# For calls that are extraction/formatting rather than judgement. Routed to the
# cheap tier first, and — via _ladder — degrades THROUGH the rest of the cheap
# tier before it ever touches the quality models' quota.
FAST_MODEL = "openai/gpt-oss-20b"

# gpt-oss is a reasoning model and its reasoning tokens are billed against
# max_tokens. Callers here cap as low as 200 tokens, so a full reasoning pass
# would consume the whole budget and return empty content — keep effort low.
REASONING_EFFORT = "low"


class GroqError(Exception):
    """Any failure talking to Groq or parsing its reply."""

    def __init__(self, message, rate_limited=False):
        super().__init__(message)
        self.rate_limited = rate_limited


def _ladder(preferred=None):
    """Models to try, in order. A caller's `preferred` model goes first but still
    falls back to the rest — so routing a call to a cheaper model never makes it
    MORE likely to fail outright when that model's daily quota runs out.

    Which tier is exhausted first depends on where the call started. A cheap call
    finishes the cheap tier before spending quality quota, so a burst of resume
    parses can't starve the persona generation that actually needs the big models;
    a quality call still ends up on the cheap tier rather than failing."""
    order = CHEAP_MODELS + QUALITY_MODELS if preferred in CHEAP_MODELS \
        else QUALITY_MODELS + CHEAP_MODELS
    chain = [preferred] if preferred else []
    return chain + [m for m in order if m != preferred]


def groq_json(prompt, max_tokens=700, temperature=0.4, json_mode=True, timeout=45,
              label="unknown", model=None):
    """One chat call -> parsed JSON (dict or list). Raises GroqError on any failure.

    json_mode adds response_format={"type":"json_object"} so the model can't wrap
    the JSON in prose; the regex fallback still guards against stray fences (and is
    the only net for callers that turn json_mode off).

    If a model's daily quota is exhausted (HTTP 429), retries the same prompt on
    the next model in the ladder before giving up.

    model overrides which model is tried FIRST (see FAST_MODEL) for calls that
    don't need the primary's quality; the rest of the ladder still applies.

    label identifies the calling feature (e.g. "evaluate_quiz") purely for the
    [groq_usage] log line below — it has no effect on the request itself. Exists
    so real per-feature, per-model token usage can be read back out of the Render
    logs instead of guessed from max_tokens caps, ahead of splitting features
    across models by task size.
    """
    if not GROQ_API_KEY:
        raise GroqError("GROQ_API_KEY is not set.")

    last_err = None
    for candidate in _ladder(model):
        try:
            return _call_model(candidate, prompt, max_tokens, temperature, json_mode, timeout, label)
        except GroqError as e:
            if not e.rate_limited:
                raise
            print(f"Groq model {candidate} rate-limited, trying next fallback")
            last_err = e
    raise last_err


# Optional sink for per-call token usage. Left as a hook rather than importing the
# database here on purpose: this module is the HTTP transport to Groq and is used
# by scripts and tests that have no Flask app or DB session. app.py wires the real
# recorder at startup; when nothing is wired, calls simply aren't recorded.
_usage_sink = None


def set_usage_sink(fn):
    """Register a callable(**usage) invoked once per successful Groq call."""
    global _usage_sink
    _usage_sink = fn


def _record_usage(**usage):
    if _usage_sink is None:
        return
    try:
        _usage_sink(**usage)
    except Exception as e:
        # Telemetry must never break the request that produced it. The Groq call
        # already succeeded; losing its accounting is strictly better than
        # failing a user's onboarding over a bookkeeping write.
        print("[groq_usage] recorder failed (usage not persisted):", e)


def _call_model(model, prompt, max_tokens, temperature, json_mode, timeout, label="unknown"):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if model.startswith("openai/gpt-oss"):
        body["reasoning_effort"] = REASONING_EFFORT

    try:
        resp = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json=body,
            timeout=timeout,
        )
        if resp.status_code == 429:
            raise GroqError(f"Rate limited on {model}: {resp.text[:300]}", rate_limited=True)
        resp.raise_for_status()
        payload = resp.json()
        usage = payload.get("usage", {})
        choice = payload["choices"][0]
        # "length" means the reply was CUT OFF at max_tokens, not that the model
        # finished. In json_mode that yields unclosed JSON, which then surfaces
        # downstream as a parse or validation failure — indistinguishable from a
        # model that simply answered badly, so an undersized cap could degrade
        # quality indefinitely without anything naming the cause. Logged rather
        # than raised: json_mode callers already fail on the broken JSON, and a
        # non-json caller may still have usable partial text.
        finish = choice.get("finish_reason")
        truncated = finish == "length"
        print(f"[groq_usage] label={label} model={model} "
              f"prompt_tokens={usage.get('prompt_tokens')} "
              f"completion_tokens={usage.get('completion_tokens')} "
              f"total_tokens={usage.get('total_tokens')} "
              f"finish={finish}"
              + (f" TRUNCATED cap={max_tokens}" if truncated else ""))
        _record_usage(
            model=model,
            label=label,
            prompt_tokens=usage.get("prompt_tokens") or 0,
            completion_tokens=usage.get("completion_tokens") or 0,
            total_tokens=usage.get("total_tokens") or 0,
            truncated=truncated,
        )
        text = choice["message"]["content"]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
            if m:
                return json.loads(m.group(1))
            raise ValueError("No JSON found in the model reply")
    except GroqError:
        raise
    except Exception as e:
        raise GroqError(str(e)) from e

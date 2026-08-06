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
# Groq's free-tier daily token quota is tracked PER MODEL, so when the primary
# model is exhausted the fallbacks still have their own, separate quotas. Primary
# is gpt-oss-120b for its 200k/day allowance; llama-3.3-70b-versatile (100k/day)
# is now the first fallback rather than the primary, and 8b-instant the last net.
FALLBACK_MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
# For calls that are extraction/formatting rather than judgement (resume parsing,
# question generation). Cheaper per token AND it spreads load across the per-model
# daily quotas, which is what actually buys free-tier headroom — the ladder below
# still falls back to the bigger models if this one is exhausted.
FAST_MODEL = "llama-3.1-8b-instant"

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
    MORE likely to fail outright when that model's daily quota runs out."""
    chain = [preferred] if preferred else []
    return chain + [m for m in [MODEL_NAME] + FALLBACK_MODELS if m != preferred]


def groq_json(prompt, max_tokens=700, temperature=0.4, json_mode=True, timeout=45,
              label="unknown", model=None):
    """One chat call -> parsed JSON (dict or list). Raises GroqError on any failure.

    json_mode adds response_format={"type":"json_object"} so the model can't wrap
    the JSON in prose; the regex fallback still guards against stray fences (and is
    the only net for callers that turn json_mode off).

    If a model's daily quota is exhausted (HTTP 429), retries the same prompt on
    the next model in FALLBACK_MODELS before giving up.

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
        print(f"[groq_usage] label={label} model={model} "
              f"prompt_tokens={usage.get('prompt_tokens')} "
              f"completion_tokens={usage.get('completion_tokens')} "
              f"total_tokens={usage.get('total_tokens')}")
        text = payload["choices"][0]["message"]["content"]
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

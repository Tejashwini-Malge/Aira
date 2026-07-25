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
MODEL_NAME = "llama-3.3-70b-versatile"
# Groq's free-tier daily token quota is tracked PER MODEL, so when the primary
# model is exhausted (100k tokens/day burns fast with real users) the fallback
# still has its own, much larger quota.
FALLBACK_MODELS = ["llama-3.1-8b-instant"]


class GroqError(Exception):
    """Any failure talking to Groq or parsing its reply."""

    def __init__(self, message, rate_limited=False):
        super().__init__(message)
        self.rate_limited = rate_limited


def groq_json(prompt, max_tokens=700, temperature=0.4, json_mode=True, timeout=45, label="unknown"):
    """One chat call -> parsed JSON (dict or list). Raises GroqError on any failure.

    json_mode adds response_format={"type":"json_object"} so the model can't wrap
    the JSON in prose; the regex fallback still guards against stray fences (and is
    the only net for callers that turn json_mode off).

    If a model's daily quota is exhausted (HTTP 429), retries the same prompt on
    the next model in FALLBACK_MODELS before giving up.

    label identifies the calling feature (e.g. "evaluate_quiz") purely for the
    [groq_usage] log line below — it has no effect on the request itself. Exists
    so real per-feature, per-model token usage can be read back out of the Render
    logs instead of guessed from max_tokens caps, ahead of splitting features
    across models by task size.
    """
    if not GROQ_API_KEY:
        raise GroqError("GROQ_API_KEY is not set.")

    last_err = None
    for model in [MODEL_NAME] + FALLBACK_MODELS:
        try:
            return _call_model(model, prompt, max_tokens, temperature, json_mode, timeout, label)
        except GroqError as e:
            if not e.rate_limited:
                raise
            print(f"Groq model {model} rate-limited, trying next fallback")
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

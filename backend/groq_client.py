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


class GroqError(Exception):
    """Any failure talking to Groq or parsing its reply."""


def groq_json(prompt, max_tokens=700, temperature=0.4, json_mode=True, timeout=45):
    """One chat call -> parsed JSON (dict or list). Raises GroqError on any failure.

    json_mode adds response_format={"type":"json_object"} so the model can't wrap
    the JSON in prose; the regex fallback still guards against stray fences (and is
    the only net for callers that turn json_mode off).
    """
    if not GROQ_API_KEY:
        raise GroqError("GROQ_API_KEY is not set.")

    body = {
        "model": MODEL_NAME,
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
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
            if m:
                return json.loads(m.group(1))
            raise ValueError("No JSON found in the model reply")
    except Exception as e:
        raise GroqError(str(e)) from e

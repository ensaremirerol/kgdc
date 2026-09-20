"""One function: chat(prompt) -> text. OpenAI-compatible endpoint from .env."""
from __future__ import annotations

import os
import re
import time

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

_RETRYABLE = ("429", "500", "502", "503", "504", "Connection error")


def chat(prompt: str, model: str | None = None, temperature: float = 0.0, attempts: int = 5) -> str:
    client = OpenAI(base_url=os.getenv("LLM_BASE_URL"), api_key=os.environ["LLM_API_KEY"])
    model = model or os.environ["LLM_MODEL"]
    for i in range(1, attempts + 1):
        try:
            r = client.chat.completions.create(
                model=model, temperature=temperature,
                messages=[{"role": "user", "content": prompt}],
            )
            return (r.choices[0].message.content or "") if r.choices else ""
        except Exception as e:  # noqa: BLE001
            if i == attempts or not any(c in str(e) for c in _RETRYABLE):
                raise
            time.sleep(10 * i)
    return ""


def strip_fences(text: str) -> str:
    m = re.search(r"```(?:turtle|ttl|json)?\s*\n(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip()

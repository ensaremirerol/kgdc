"""One function: chat(prompt) -> text. OpenAI-compatible endpoint from .env."""
from __future__ import annotations

import os
import re
import threading
import time
from collections import defaultdict

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

_RETRYABLE = ("429", "500", "502", "503", "504", "Connection error", "empty choices", "timed out", "Timeout")


BIG = "big"   # pass model=BIG for the orchestrator (segmentation plan, final merge); LLM_BIG_TIMEOUT defaults to 600 s (a merge writes the whole graph)


class MalformedToolCall(Exception):
    """The model produced a tool call the provider could not parse (not transient)."""

# Per-call usage: {"role", "model", "prompt_tokens", "completion_tokens", "cost"}.
# cost comes from the provider when it reports one (OpenRouter with usage
# accounting) else from LLM[_BIG]_COST_PER_MTOK="<in>,<out>" (USD per 1M tokens), else None.
USAGE: list[dict] = []
_ULOCK = threading.Lock()


def _cost(role: str, usage, reported) -> float | None:
    if reported is not None:
        return float(reported)
    price = os.getenv(role + "COST_PER_MTOK") or (os.getenv("LLM_COST_PER_MTOK") if role == "LLM_" else None)
    if not price:
        return None
    pin, pout = (float(x) for x in price.split(","))
    return (usage.prompt_tokens * pin + usage.completion_tokens * pout) / 1e6


def usage_summary() -> dict:
    """Totals per role and overall: calls, prompt/completion tokens, cost (None if any call lacked a price)."""
    out: dict = defaultdict(lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0, "model": None})
    for u in USAGE:
        for key in (u["role"], "total"):
            r = out[key]
            r["calls"] += 1; r["prompt_tokens"] += u["prompt_tokens"]; r["completion_tokens"] += u["completion_tokens"]
            r["cost"] = None if (r["cost"] is None or u["cost"] is None) else r["cost"] + u["cost"]
            if key != "total":
                r["model"] = u["model"]
    return dict(out)


def chat(prompt: str, model: str | None = None, temperature: float = 0.0, attempts: int = 5, max_tokens: int | None = None) -> str:
    # Workers: LLM_*. Orchestrator: LLM_BIG_*, each falling back to the worker
    # setting, so the two roles can live on different endpoints/keys.
    role = "LLM_BIG_" if model == BIG else "LLM_"
    env = lambda k: os.getenv(role + k) or os.environ["LLM_" + k]
    headers = {}
    if os.getenv(role + "BASIC_AUTH"):   # e.g. an Ollama behind an auth proxy: "user:password"
        import base64
        headers["Authorization"] = "Basic " + base64.b64encode(os.environ[role + "BASIC_AUTH"].encode()).decode()
    client = OpenAI(base_url=env("BASE_URL"), api_key=env("API_KEY"), timeout=float(os.getenv(role + "TIMEOUT") or ("600" if role == "LLM_BIG_" else os.getenv("LLM_TIMEOUT", "90"))), max_retries=0, default_headers=headers)
    model = env("MODEL") if model in (None, BIG) else model
    for i in range(1, attempts + 1):
        try:
            extra = {"usage": {"include": True}}   # OpenRouter: report cost; ignored elsewhere
            if os.getenv(role + "PROVIDER_SORT"):   # OpenRouter: e.g. "throughput" to avoid slow providers
                extra["provider"] = {"sort": os.environ[role + "PROVIDER_SORT"], "allow_fallbacks": True}
            kw = {"max_tokens": int(os.environ[role + "MAX_TOKENS"])} if role == "LLM_BIG_" and os.getenv("LLM_BIG_MAX_TOKENS") else {}   # a merge writes the whole graph: endpoints cap output by default
            if max_tokens:   # the caller knows the reply is short (edit-list merge): leave the context to the input
                kw["max_tokens"] = max_tokens
            r = client.chat.completions.create(
                model=model, temperature=temperature,
                messages=[{"role": "user", "content": prompt}],
                extra_body=extra, **kw,
            )
            if not r.choices:   # OpenRouter: upstream error delivered as 200 with an `error` field
                err = (r.model_dump().get("error") or {})
                msg = str(err.get("message", "?"))
                if any(k in msg for k in ("Extra data", "tool-call", "tool call", "JSON", "Expecting")):
                    raise MalformedToolCall(msg[:200])   # deterministic: retrying the same prompt reproduces it
                raise RuntimeError(f"empty choices ({msg[:160]}; id={getattr(r, 'id', '?')})")
            if getattr(r, "usage", None):
                with _ULOCK:
                    USAGE.append({"role": "orchestrator" if role == "LLM_BIG_" else "worker", "model": model,
                                  "prompt_tokens": r.usage.prompt_tokens or 0,
                                  "completion_tokens": r.usage.completion_tokens or 0,
                                  "cost": _cost(role, r.usage, getattr(r.usage, "cost", None))})
            return r.choices[0].message.content or ""
        except Exception as e:  # noqa: BLE001
            if i == attempts or not any(c in str(e) for c in _RETRYABLE):
                raise
            time.sleep(10 * i)
    return ""


def chat_messages(messages: list[dict], tools: list[dict] | None = None, model: str | None = None,
                  temperature: float = 0.0, attempts: int = 5):
    """Multi-turn / tool-calling variant of chat(): returns the assistant message object."""
    role = "LLM_BIG_" if model == BIG else "LLM_"
    env = lambda k: os.getenv(role + k) or os.environ["LLM_" + k]
    headers = {}
    if os.getenv(role + "BASIC_AUTH"):   # e.g. an Ollama behind an auth proxy: "user:password"
        import base64
        headers["Authorization"] = "Basic " + base64.b64encode(os.environ[role + "BASIC_AUTH"].encode()).decode()
    client = OpenAI(base_url=env("BASE_URL"), api_key=env("API_KEY"), timeout=float(os.getenv(role + "TIMEOUT") or ("600" if role == "LLM_BIG_" else os.getenv("LLM_TIMEOUT", "90"))), max_retries=0, default_headers=headers)
    model = env("MODEL") if model in (None, BIG) else model
    for i in range(1, attempts + 1):
        try:
            extra = {"usage": {"include": True}}
            if os.getenv(role + "PROVIDER_SORT"):
                extra["provider"] = {"sort": os.environ[role + "PROVIDER_SORT"], "allow_fallbacks": True}
            # one tool call per turn: small models glue several into one malformed
            # arguments blob, and some providers (Groq) reject parallel calls outright
            kw = {"tools": tools, "tool_choice": os.getenv("KGDC_TOOL_CHOICE", "required"), "parallel_tool_calls": False} if tools else {}
            if os.getenv("LLM_MAX_TOKENS"):
                kw["max_tokens"] = int(os.environ["LLM_MAX_TOKENS"])
            r = client.chat.completions.create(model=model, temperature=temperature, messages=messages, extra_body=extra, **kw)
            if not r.choices:   # OpenRouter: upstream error delivered as 200 with an `error` field
                err = (r.model_dump().get("error") or {})
                msg = str(err.get("message", "?"))
                if any(k in msg for k in ("Extra data", "tool-call", "tool call", "JSON", "Expecting")):
                    raise MalformedToolCall(msg[:200])   # deterministic: retrying the same prompt reproduces it
                raise RuntimeError(f"empty choices ({msg[:160]}; id={getattr(r, 'id', '?')})")
            if getattr(r, "usage", None):
                with _ULOCK:
                    USAGE.append({"role": "orchestrator" if role == "LLM_BIG_" else "worker", "model": model,
                                  "prompt_tokens": r.usage.prompt_tokens or 0,
                                  "completion_tokens": r.usage.completion_tokens or 0,
                                  "cost": _cost(role, r.usage, getattr(r.usage, "cost", None))})
            return r.choices[0].message
        except Exception as e:  # noqa: BLE001
            if i == attempts or not any(c in str(e) for c in _RETRYABLE):
                raise
            time.sleep(10 * i)
    raise RuntimeError("unreachable")


def strip_fences(text: str) -> str:
    m = re.search(r"```(?:turtle|ttl|json)?\s*\n(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip()

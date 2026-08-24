"""One call() over three backends.

  anthropic  - Messages API, when ANTHROPIC_API_KEY / LLM_API_KEY=sk-ant-* is set
  openai     - any /chat/completions endpoint (LLM_BASE_URL + LLM_API_KEY)
  claude-cli - `claude -p`, used when no key is configured but the CLI is here

The third backend is what makes a local run free: the same prompts that CI runs
with a key also run on a laptop against an already-authenticated Claude Code.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time

from . import net
from .util import log

KEY = (os.environ.get("LLM_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
BASE = (os.environ.get("LLM_BASE_URL") or "").strip().rstrip("/")
MODEL = (os.environ.get("LLM_MODEL") or "").strip()
FORCE = (os.environ.get("LLM_PROVIDER") or "").strip().lower()

DEFAULT_ANTHROPIC = "claude-opus-5"
DEFAULT_OPENAI = "gpt-4o-mini"
DEFAULT_CLI = "opus"


def provider() -> str:
    if FORCE:
        return FORCE
    if KEY:
        if KEY.startswith("sk-ant-") or "anthropic" in BASE:
            return "anthropic"
        return "openai"
    if shutil.which("claude"):
        return "claude-cli"
    return "none"


def model_name() -> str:
    p = provider()
    if MODEL:
        return MODEL
    return {"anthropic": DEFAULT_ANTHROPIC, "openai": DEFAULT_OPENAI,
            "claude-cli": DEFAULT_CLI}.get(p, "none")


def available() -> bool:
    return provider() != "none"


def safe_jobs() -> int:
    """How many of these calls may run at once.

    The HTTP backends are happy in parallel. `claude -p` is not: several
    headless sessions at once exit non-zero with no message, so the CLI backend
    runs one at a time even when a larger --jobs was asked for."""
    return 1 if provider() == "claude-cli" else 4


def call(system: str, user: str, *, max_tokens: int = 6000,
         temperature: float = 0.3) -> str:
    p = provider()
    if p == "anthropic":
        base = BASE if "anthropic" in BASE else "https://api.anthropic.com"
        r = net.post_json(base + "/v1/messages", {
            "model": model_name(), "max_tokens": max_tokens,
            "temperature": temperature, "system": system,
            "messages": [{"role": "user", "content": user}]},
            {"x-api-key": KEY, "anthropic-version": "2023-06-01"})
        return "".join(b.get("text", "") for b in r.get("content", []))
    if p == "openai":
        r = net.post_json((BASE or "https://api.openai.com/v1") + "/chat/completions", {
            "model": model_name(), "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]},
            {"Authorization": "Bearer " + KEY})
        return (r.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    if p == "claude-cli":
        return _cli(system, user)
    raise RuntimeError("no LLM backend: set LLM_API_KEY or install the claude CLI")


# The CLI has no retry of its own, so a 529 from the provider surfaces as a
# plain non-zero exit and would drop the episode. Back off and try again, the
# same way post_json() does for the HTTP backends.
_TRANSIENT = re.compile(r"(529|overloaded|rate.?limit|too many requests|"
                        r"502|503|504|timed? ?out|connection reset)", re.I)


def _cli(system: str, user: str, *, tries: int = 4) -> str:
    # stdin carries the payload so a long transcript never hits ARG_MAX.
    cmd = ["claude", "-p", "--model", model_name(),
           "--append-system-prompt", system, "--max-turns", "1"]
    last = ""
    for i in range(tries):
        try:
            r = subprocess.run(cmd, input=user, capture_output=True, text=True, timeout=1800)
        except subprocess.TimeoutExpired:
            last = "timed out after 1800s"
        else:
            if r.returncode == 0 and (r.stdout or "").strip():
                return r.stdout
            last = ((r.stderr or "").strip() or (r.stdout or "").strip() or
                    f"exit {r.returncode} with no output — the CLI does this when "
                    f"several headless sessions run at once")
        if i < tries - 1 and _TRANSIENT.search(last):
            wait = 20 * (2 ** i)
            log(f"    provider busy ({last[:70]}) — retrying in {wait}s "
                f"({i + 1}/{tries - 1})")
            time.sleep(wait)
            continue
        break
    raise RuntimeError(f"claude cli failed: {last[:300]}")


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


JSON_RULE = ("\n\nRespond with a single JSON object and nothing else. "
             "Inside JSON strings, write Chinese quotation as 「」 and escape any "
             "literal double quote as \\\" — an unescaped \" breaks the whole object.")


def call_json(system: str, user: str, *, retries: int = 2, **kw) -> dict:
    """Ask for JSON and actually get it.

    Models fence it, prefix it, or trail prose after it — all three are
    recovered. The remaining failure is a genuinely malformed object (usually an
    unescaped quote inside a Chinese sentence), so the error is handed back and
    the model gets another go rather than losing the whole episode."""
    prompt = user
    last = ""
    for attempt in range(retries + 1):
        raw = call(system + JSON_RULE, prompt, **kw)
        for cand in _candidates(raw):
            try:
                v = json.loads(cand)
                if isinstance(v, dict):
                    return v
            except Exception as ex:
                last = str(ex)
        if attempt < retries:
            log(f"    invalid JSON ({last[:70]}) — asking again ({attempt + 1}/{retries})")
            prompt = (user + "\n\n你上一次的输出不是合法 JSON，报错是："
                      + last[:200]
                      + "\n请重新输出，只输出一个合法 JSON 对象。中文引号一律用「」，"
                        "字符串里的英文双引号必须转义成 \\\".")
    raise ValueError("model did not return JSON after retries: "
                     + raw[:300].replace("\n", " "))


def _candidates(raw: str):
    raw = raw.strip()
    yield raw
    m = _FENCE.search(raw)
    if m:
        yield m.group(1).strip()
    i, j = raw.find("{"), raw.rfind("}")
    if i != -1 and j > i:
        yield raw[i:j + 1]

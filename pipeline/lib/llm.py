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
# 分工用不同模型：选题是廉价分类，深读要质量，成稿评分最好换一家——
# 同一个模型给自己的作业打分，天然偏袒。OpenRouter 这类聚合端点一个 key
# 就能切换，所以这个拆分几乎没有额外成本。
MODEL_TRIAGE = (os.environ.get("LLM_MODEL_TRIAGE") or "").strip()
MODEL_REVIEW = (os.environ.get("LLM_MODEL_REVIEW") or "").strip()
FORCE = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
# Anthropic accepts two credential shapes on different headers. An API key goes
# on x-api-key; an OAuth token (from `ant auth login` / Claude Code) goes on
# Authorization: Bearer plus a beta header. Sending one on the other's header
# returns "API key is invalid", which reads like a bad key rather than a bad
# header — so auto-detect, and let LLM_AUTH override.
AUTH = (os.environ.get("LLM_AUTH") or "").strip().lower()


def auth_mode() -> str:
    if AUTH in ("bearer", "oauth"):
        return "bearer"
    if AUTH in ("api-key", "apikey", "x-api-key"):
        return "api-key"
    # OAuth access tokens are not sk-ant-api* keys; treat those prefixes as OAuth.
    if KEY.startswith(("sk-ant-oat", "sk-ant-ort", "sk-ant-sid")):
        return "bearer"
    return "api-key"


def anthropic_headers() -> dict:
    if auth_mode() == "bearer":
        return {"Authorization": "Bearer " + KEY, "anthropic-version": "2023-06-01",
                "anthropic-beta": "oauth-2025-04-20"}
    return {"x-api-key": KEY, "anthropic-version": "2023-06-01"}

DEFAULT_ANTHROPIC = "claude-opus-5"
DEFAULT_OPENAI = "gpt-4o-mini"
DEFAULT_CLI = "opus"


class AuthError(RuntimeError):
    """The credentials are wrong or pointed at the wrong service. Not transient —
    retrying or moving to the next episode just repeats the failure."""


def provider() -> str:
    """Anthropic unless a base URL says otherwise.

    The earlier rule was "sk-ant-* means Anthropic, anything else means OpenAI",
    which silently shipped a perfectly good Anthropic key to api.openai.com and
    got a 401. This project's default model is claude-opus-5, so Anthropic is
    the default; an OpenAI-compatible endpoint has to be named explicitly with
    LLM_BASE_URL, which is what the README asks for.
    """
    if FORCE:
        return FORCE
    if KEY:
        if BASE and "anthropic" not in BASE:
            return "openai"
        return "anthropic"
    if shutil.which("claude"):
        return "claude-cli"
    return "none"


def endpoint() -> str:
    p = provider()
    if p == "anthropic":
        return (BASE if "anthropic" in BASE else "https://api.anthropic.com") + "/v1/messages"
    if p == "openai":
        return (BASE or "https://api.openai.com/v1") + "/chat/completions"
    return p


def model_name(role: str = "digest") -> str:
    """role: digest | triage | review。未单独配置时回落到 LLM_MODEL。"""
    override = {"triage": MODEL_TRIAGE, "review": MODEL_REVIEW}.get(role, "")
    if override:
        return override
    if MODEL:
        return MODEL
    return {"anthropic": DEFAULT_ANTHROPIC, "openai": DEFAULT_OPENAI,
            "claude-cli": DEFAULT_CLI}.get(provider(), "none")


def roles() -> dict:
    return {r: model_name(r) for r in ("digest", "triage", "review")}


def available() -> bool:
    return provider() != "none"


def safe_jobs() -> int:
    """How many of these calls may run at once.

    The HTTP backends are happy in parallel. `claude -p` is not: several
    headless sessions at once exit non-zero with no message, so the CLI backend
    runs one at a time even when a larger --jobs was asked for."""
    return 1 if provider() == "claude-cli" else 4


def call(system: str, user: str, *, max_tokens: int = 6000,
         temperature: float = 0.3, want_json: bool = False,
         role: str = "digest") -> str:
    p = provider()
    model = model_name(role)
    if p == "anthropic":
        base = BASE if "anthropic" in BASE else "https://api.anthropic.com"
        r = _guard(lambda: net.post_json(base + "/v1/messages", {
            "model": model, "max_tokens": max_tokens,
            "temperature": temperature, "system": system,
            "messages": [{"role": "user", "content": user}]},
            anthropic_headers()))
        return "".join(b.get("text", "") for b in r.get("content", []))
    if p == "openai":
        return _openai(system, user, max_tokens, temperature, model,
                       json_mode=want_json and _json_mode[0])
    if p == "claude-cli":
        return _cli(system, user, model=model)
    raise RuntimeError("no LLM backend: set LLM_API_KEY or install the claude CLI")


# The CLI has no retry of its own, so a 529 from the provider surfaces as a
# plain non-zero exit and would drop the episode. Back off and try again, the
# same way post_json() does for the HTTP backends.
_TRANSIENT = re.compile(r"(529|overloaded|rate.?limit|too many requests|"
                        r"502|503|504|timed? ?out|connection reset)", re.I)


# DeepSeek, Qwen, GLM and friends support response_format=json_object, which
# removes most "model did not return JSON" retries. A few OpenAI-compatible
# proxies reject the field, so the first rejection turns it off for the process
# rather than failing the episode.
_json_mode = [True]


def _openai(system: str, user: str, max_tokens: int, temperature: float,
            model: str, *, json_mode: bool) -> str:
    url = (BASE or "https://api.openai.com/v1") + "/chat/completions"
    body = {"model": model, "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    h = {"Authorization": "Bearer " + KEY}
    if "openrouter" in url:
        # OpenRouter 用这两个头做用量归属，也让请求不容易被当成匿名滥用
        h["HTTP-Referer"] = "https://ourword.ai/podcast/"
        h["X-Title"] = "OurWord Podcast"
    try:
        r = _guard(lambda: net.post_json(url, body, h))
    except AuthError:
        raise
    except Exception as ex:
        if json_mode and re.search(r"response_format|json_object|400", str(ex), re.I):
            log("    这个端点不接受 response_format，本次起改用纯提示词约束 JSON")
            _json_mode[0] = False
            return _openai(system, user, max_tokens, temperature, model, json_mode=False)
        raise
    return (r.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""


def _cli(system: str, user: str, *, model: str = "", tries: int = 4) -> str:
    # stdin carries the payload so a long transcript never hits ARG_MAX.
    #
    # No --max-turns: at 1 the CLI can spend the single turn on setup and exit
    # with "Reached max turns" before writing anything — it happened on sonnet
    # while opus got away with it. Tools are switched off instead, which is the
    # actual intent (one generation, no file access, no wasted turns).
    cmd = ["claude", "-p", "--model", model or model_name(),
           "--append-system-prompt", system,
           "--allowed-tools", ""]
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


_AUTH = re.compile(r"HTTP (401|403)|invalid[ _-]?(x-)?api[ _-]?key|"
                   r"authentication|unauthorized|incorrect api key", re.I)


def _guard(fn):
    """Turn a credentials failure into AuthError so the caller stops instead of
    repeating it on every remaining episode."""
    try:
        return fn()
    except Exception as ex:
        if _AUTH.search(str(ex)):
            other = "bearer" if auth_mode() == "api-key" else "api-key"
            raise AuthError(
                f"{endpoint()} 拒绝了这套凭证：{str(ex)[:170]}\n"
                f"  provider={provider()} model={model_name()} auth={auth_mode()}\n"
                f"  逐个排掉：\n"
                f"  1) 这是 Claude Code / ant auth 的 OAuth token，而不是 API key？\n"
                f"     那就设 LLM_AUTH={other}（换 header，不换 key）。\n"
                f"  2) 这是 console.anthropic.com 建的 API key（sk-ant-api…）？\n"
                f"     确认没有多余空格换行，且没被吊销、所属组织有额度。\n"
                f"  3) 这是别家的 key？必须同时设 LLM_BASE_URL 和 LLM_MODEL。\n"
                f"  本地跑 `python3 pipeline/whoami.py` 会把两种 header 都试一遍，\n"
                f"  只报哪种能用，不打印密钥。"
            ) from None
        raise


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
        raw = call(system + JSON_RULE, prompt, want_json=True, **kw)
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

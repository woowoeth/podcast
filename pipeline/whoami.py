#!/usr/bin/env python3
"""Say which credential shape works, without ever printing the credential.

    LLM_API_KEY=… python3 pipeline/whoami.py

Anthropic takes an API key on `x-api-key` and an OAuth token on
`Authorization: Bearer` (+ a beta header). Sending one on the other's header
returns "API key is invalid", which looks like a bad key instead of a bad
header. This tries both and tells you which to configure.
"""
from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib import net                                            # noqa: E402
from lib.util import log                                       # noqa: E402

KEY = (os.environ.get("LLM_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
BASE = (os.environ.get("LLM_BASE_URL") or "").strip().rstrip("/")
MODEL = (os.environ.get("LLM_MODEL") or "claude-opus-5").strip()

PING = {"max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}


def shape() -> str:
    n = len(KEY)
    if not KEY:
        return "（未设置）"
    head = KEY[:11] if n > 20 else KEY[:4]
    return f"前缀 {head}… 长度 {n}"


def try_anthropic(mode: str) -> tuple[bool, str]:
    base = BASE if "anthropic" in BASE else "https://api.anthropic.com"
    if mode == "bearer":
        h = {"Authorization": "Bearer " + KEY, "anthropic-version": "2023-06-01",
             "anthropic-beta": "oauth-2025-04-20"}
    else:
        h = {"x-api-key": KEY, "anthropic-version": "2023-06-01"}
    try:
        r = net.post_json(base + "/v1/messages", dict(PING, model=MODEL), h,
                          timeout=60, tries=1)
        txt = "".join(b.get("text", "") for b in r.get("content", []))
        return True, f"OK · 模型回了 {len(txt)} 字符"
    except Exception as ex:
        return False, str(ex)[:150]


def try_openai() -> tuple[bool, str]:
    url = (BASE or "https://api.openai.com/v1") + "/chat/completions"
    try:
        r = net.post_json(url, {"model": os.environ.get("LLM_MODEL") or "gpt-4o-mini",
                                "max_tokens": 16,
                                "messages": [{"role": "user", "content": "hi"}]},
                          {"Authorization": "Bearer " + KEY}, timeout=60, tries=1)
        return True, f"OK · {len(r.get('choices') or [])} choice(s)"
    except Exception as ex:
        return False, str(ex)[:150]


def main() -> int:
    log("凭证探测（不会打印密钥本身）")
    log(f"  LLM_API_KEY  : {shape()}")
    log(f"  LLM_BASE_URL : {BASE or '（未设置 → 走 Anthropic）'}")
    log(f"  LLM_MODEL    : {MODEL}")
    if not KEY:
        log("\n没有 key 可以测。设一个 LLM_API_KEY 再跑。")
        return 2

    log("\n试 Anthropic · x-api-key（console 建的 API key 用这个）")
    ok_api, msg_api = try_anthropic("api-key")
    log(("  ✓ " if ok_api else "  ✗ ") + msg_api)

    log("\n试 Anthropic · Authorization: Bearer + oauth beta（ant auth / Claude Code 的 token 用这个）")
    ok_oauth, msg_oauth = try_anthropic("bearer")
    log(("  ✓ " if ok_oauth else "  ✗ ") + msg_oauth)

    ok_oai = False
    if BASE:
        log(f"\n试 OpenAI 兼容 · {BASE}/chat/completions")
        ok_oai, msg = try_openai()
        log(("  ✓ " if ok_oai else "  ✗ ") + msg)

    log("\n" + "—" * 52)
    if ok_api:
        log("这套凭证是 Anthropic API key。不用设 LLM_AUTH，也不要设 LLM_BASE_URL。")
        return 0
    if ok_oauth:
        log("这套凭证是 OAuth token。加一个 secret：LLM_AUTH=bearer")
        log("  gh secret set LLM_AUTH --repo woowoeth/podcast   # 值填 bearer")
        log("  注意 OAuth token 会过期，过期后定时任务会红灯——长期跑建议换 API key。")
        return 0
    if ok_oai:
        log("这套凭证走 OpenAI 兼容端点。保持 LLM_BASE_URL 和 LLM_MODEL 都设好。")
        return 0
    log("两种 header 都被拒了。这不是 header 的问题，是这套凭证本身的问题：")
    log("  · 复制时带了空格或换行？")
    log("  · 已被吊销 / 所属组织没额度？")
    log("  · 其实是别家的 key？那要同时设 LLM_BASE_URL 和 LLM_MODEL。")
    log("  去 console.anthropic.com 新建一个 sk-ant-api… 的 key 最省事。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

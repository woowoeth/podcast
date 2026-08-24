#!/usr/bin/env python3
"""Check that this deployment can actually do its job.

    python3 pipeline/selftest.py

Run it after setting secrets. It answers the only questions that matter before a
scheduled run: can we reach the feeds, can we get a transcript, can we call a
model, and is audio transcription switched on. Nothing here writes to data/.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib import feeds, llm, net, transcript as T                # noqa: E402
from lib.util import log                                        # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
OK, WARN, BAD = "  ok  ", "  !!  ", " FAIL "
problems: list[str] = []
warnings: list[str] = []


def fail(msg: str) -> None:
    log(BAD + msg)
    problems.append(msg)


def warn(msg: str) -> None:
    log(WARN + msg)
    warnings.append(msg)


def check_tools() -> None:
    import shutil
    log("\n— 本机工具 —")
    for name, why, hard in (("yt-dlp", "第 4 层：YouTube 字幕", False),
                            ("ffmpeg", "第 5 层：切分超长音频", False)):
        if shutil.which(name):
            log(OK + f"{name} 可用（{why}）")
        else:
            warn(f"{name} 缺失 → {why} 不可用")
    try:
        import truststore                                        # noqa: F401
        log(OK + "truststore 可用（用系统信任库，能穿过 TLS 拦截代理）")
    except ImportError:
        try:
            import certifi                                       # noqa: F401
            log(OK + "certifi 可用（无 truststore；本机若有 TLS 代理会握手失败）")
        except ImportError:
            warn("既无 truststore 也无 certifi，HTTPS 只能靠标准库自带根证书")


def check_model() -> None:
    log("\n— 生成后端 —")
    p = llm.provider()
    if p == "none":
        fail("没有任何生成后端：CI 里要设 LLM_API_KEY；本机装 claude CLI 并登录即可")
        return
    log(OK + f"后端 {p}，模型 {llm.model_name()}"
        + ("（本机 CLI：不产生 API 账单，但会花你的 Claude 订阅额度）"
           if p == "claude-cli" else ""))
    if p == "claude-cli":
        log("       注意：GitHub Actions 的 runner 上没有 claude CLI，"
            "定时任务必须配 LLM_API_KEY")
    try:
        r = llm.call_json("You are terse.", 'Return exactly {"ok":true}.', max_tokens=100)
        if r.get("ok") is True:
            log(OK + "模型往返正常，JSON 解析正常")
        else:
            warn(f"模型有响应但内容意外：{r}")
    except Exception as ex:
        fail(f"模型调用失败：{type(ex).__name__}: {str(ex)[:140]}")


def check_asr() -> None:
    log("\n— 第 5 层：音频转写 —")
    if not T.ASR_KEY:
        warn("TRANSCRIBE_API_KEY 未设置 → 第 5 层关闭。"
             "只发前四层能拿到文稿的节目；纯音频的中文播客和 Lenny's、Dwarkesh "
             "这类没有官方文稿的节目会整块缺失")
        return
    log(OK + f"key 已设置，端点 {T.ASR_BASE}，模型 {T.ASR_MODEL}")
    try:
        # A 0.4s silent WAV: enough to prove auth and the route, costs nothing.
        import struct
        n = 6400
        pcm = b"\x00\x00" * n
        hdr = (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
               + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
               + b"data" + struct.pack("<I", len(pcm)))
        r = net.post_multipart(T.ASR_BASE + "/audio/transcriptions",
                               {"model": T.ASR_MODEL, "response_format": "verbose_json"},
                               "probe.wav", hdr + pcm,
                               {"Authorization": "Bearer " + T.ASR_KEY},
                               timeout=90, tries=1)
        log(OK + f"转写端点可用（返回 {len(r.get('segments') or [])} 段，静音样本本该为 0-1 段）")
    except Exception as ex:
        fail(f"转写端点不可用：{type(ex).__name__}: {str(ex)[:140]}")


def check_sources() -> None:
    log("\n— 信源 —")
    path = ROOT / "data" / "sources.json"
    if not path.exists():
        fail("data/sources.json 不存在，先跑 pipeline/resolve_sources.py --check")
        return
    srcs = json.loads(path.read_text())["sources"]
    log(OK + f"{len(srcs)} 档信源在册")
    # Probe a handful across all three categories rather than all 49.
    picks, seen = [], set()
    for s in srcs:
        if s.get("tier") == 1 and s["cat"] not in seen:
            picks.append(s)
            seen.add(s["cat"])
    dead = 0
    for s in picks:
        try:
            eps = feeds.fetch(s, cache_ttl=0)
            log(OK + f"{s['id']:<14} {len(eps):>4} 集，最新：{eps[0]['title'][:44]}")
        except Exception as ex:
            fail(f"{s['id']}: {type(ex).__name__}: {str(ex)[:90]}")
            dead += 1
    if not dead:
        log(f"       抽查了 {len(picks)} 档（每个分类一档 T1）；"
            f"要体检全部 49 档：pipeline/resolve_sources.py --check")


def check_transcript_path() -> None:
    log("\n— 端到端取稿（不调模型）—")
    srcs = {s["id"]: s for s in json.loads((ROOT / "data" / "sources.json").read_text())["sources"]}
    s = srcs.get("oddlots")            # ships official transcripts for every episode
    if not s:
        return
    try:
        ep = feeds.fetch(s, cache_ttl=0)[0]
    except Exception as ex:
        fail(f"取 oddlots 失败：{type(ex).__name__}")
        return
    tr = T.acquire(ep, s.get("lang", "en"), allow=("feed",), src=s)
    if tr:
        log(OK + f"第 1 层可用：{tr['words']} 词，{tr['wpm']} wpm，{len(tr['segments'])} 段")
    else:
        fail("连自带官方逐字稿的节目都取不到文稿——网络或解析出了问题")


def main() -> int:
    log("原声 · 自检")
    check_tools()
    check_sources()
    check_model()
    check_asr()
    check_transcript_path()
    log("\n" + "—" * 52)
    if problems:
        log(f"{len(problems)} 项阻塞，跑定时任务前必须修：")
        for p in problems:
            log("  · " + p)
    if warnings:
        log(f"{len(warnings)} 项降级（能跑，但覆盖率受损）：")
        for w in warnings:
            log("  · " + w)
    if not problems and not warnings:
        log("全部通过。")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())

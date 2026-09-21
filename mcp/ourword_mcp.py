#!/usr/bin/env python3
"""原声（ourword.ai/podcast）的 MCP 服务器：把播客深读开放给 agent 查。

**为什么值得单独做一个服务器，而不是让 agent 去爬站。**
站上已经有 llms.txt、llms-full.txt、sitemap 和每集的 JSON，但那是给「读一遍」
准备的：llms-full.txt 6.9 MB，agent 要回答「谁讲过 reward hacking」只能整份读进
上下文。这个服务器把同一批数据变成可查的：先按关键词筛出几条，再按 slug 取那一集
的要点、金句和数字 —— 每条都带回到原声那一秒的时间戳。

**只用标准库。** MCP 就是 stdio 上的 JSON-RPC，没有必须依赖第三方 SDK 的理由；
而少一层依赖，别人 `uvx`／`python3 ourword_mcp.py` 就能跑，不用先解决安装。

**数据从线上取，不打包进来。** 站每天都在长（今天 740 集），
把数据塞进服务器就等于发行当天的快照，用的人拿到的永远是旧的。

**不给逐字稿。** 逐字稿是第三方版权内容，站上任何一份文件里都没有，
这里也不会有。给出去的是我们自己写的判断、可核对的数字、带署名的短引用，
以及回到原音频那一秒的链接 —— 引用我们的人能自己去核。
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.request

# 留一个覆盖口：本地验证、或者别人自己部署一份时用。
# 默认指向线上，装上就能用，不用先配什么。
SITE = os.environ.get("OURWORD_SITE", "https://ourword.ai/podcast").rstrip("/")
UA = "ourword-mcp/1.0 (+https://ourword.ai/podcast/)"
CACHE_TTL = 900                      # 索引缓存 15 分钟：站一天更新几次，不用每问一次都拉
_cache: dict = {"at": 0.0, "index": None}


def _get(url: str, timeout: int = 30) -> bytes:
    # 本地路径和 HTTP 的转义规则不一样：中文 slug 走 HTTP 要百分号编码，
    # 而编码过的路径在磁盘上根本不存在。所以**只在真走网络时才编码**。
    if url.startswith("file://") or url.startswith("/"):
        return pathlib.Path(url.replace("file://", "")).read_bytes()
    url = urllib.parse.quote(url, safe=":/?&=#%")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def index() -> dict:
    if _cache["index"] is not None and time.time() - _cache["at"] < CACHE_TTL:
        return _cache["index"]
    d = json.loads(_get(f"{SITE}/api.json"))
    _cache.update(at=time.time(), index=d)
    return d


def _hms(t) -> str:
    t = int(t or 0)
    return (f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600
            else f"{t // 60}:{t % 60:02d}")


# ------------------------------------------------------------------ 四个工具

def t_search(query: str = "", show: str = "", cat: str = "",
             limit: int = 10) -> dict:
    """按关键词找集。命中的字段一并回报 —— 用的人要知道为什么这条被选中。"""
    q = [w for w in re.split(r"\s+", (query or "").strip().lower()) if w]
    rows = []
    for e in index()["episodes"]:
        if show and show.lower() not in f"{e.get('show') or ''} {e.get('show_id') or ''}".lower():
            continue
        if cat and (e.get("cat") or "") != cat:
            continue
        hay = " ".join(str(e.get(k) or "") for k in
                       ("title", "dek", "show", "original_title")).lower()
        hay += " " + " ".join(e.get("tags") or []).lower()
        if q and not all(w in hay for w in q):
            continue
        rows.append({k: e[k] for k in
                     ("slug", "title", "dek", "show", "published", "cat",
                      "minutes", "tags", "score", "url")})
    return {"matched": len(rows), "episodes": rows[:max(1, min(limit, 50))]}


def t_get_episode(slug: str) -> dict:
    """一集的全部结构：要点、金句、数字、术语，每条带时间戳。

    时间戳是这个站的全部意义 —— 引用的人能顺着回到原声那一秒去核。
    """
    e = json.loads(_get(f"{SITE}/data/episodes/{slug}.json"))
    d = e.get("digest") or {}
    return {
        "slug": e.get("slug"), "title": d.get("title"), "dek": d.get("dek"),
        "url": f"{SITE}/p/{e.get('slug')}/",
        "show": e.get("source"), "show_url": f"{SITE}/s/{e.get('source_id')}/",
        "original_title": e.get("title_original"),
        "published": (e.get("published") or "")[:10],
        "minutes": int((e.get("duration") or 0) // 60) or None,
        "listen": e.get("audio") or e.get("link"),
        "review_score": (e.get("review") or {}).get("score"),
        "points": [{"at": _hms(p.get("t")), "seconds": p.get("t"),
                    "head": p.get("h"), "body": p.get("body")}
                   for p in (d.get("points") or [])],
        "quotes": [{"at": _hms(q.get("t")), "seconds": q.get("t"),
                    "speaker": q.get("spk"), "zh": q.get("zh"),
                    "original": q.get("raw")}
                   for q in (d.get("quotes") or [])],
        "facts": [{"at": _hms(f.get("t")), "metric": f.get("k"), "value": f.get("v")}
                  for f in (d.get("facts") or [])],
        "terms": d.get("terms") or [],
        "note": ("Points, quotes and numbers are our own written analysis of the "
                 "episode. Full transcripts are not redistributed — follow "
                 "`listen` and the timestamps to check anything against the audio."),
    }


def t_list_shows(cat: str = "") -> dict:
    """在册的节目，带各自已深读的篇数 —— 从索引里数出来，不另存一份会过期的表。"""
    by: dict = {}
    for e in index()["episodes"]:
        if cat and (e.get("cat") or "") != cat:
            continue
        k = e.get("show_id")
        if not k:
            continue
        r = by.setdefault(k, {"id": k, "show": e.get("show"),
                              "url": e.get("show_url"), "cat": e.get("cat"),
                              "episodes": 0, "latest": ""})
        r["episodes"] += 1
        r["latest"] = max(r["latest"], e.get("published") or "")
    rows = sorted(by.values(), key=lambda r: -r["episodes"])
    return {"count": len(rows), "shows": rows}


def t_latest(cat: str = "", limit: int = 10) -> dict:
    rows = [e for e in index()["episodes"] if not cat or e.get("cat") == cat]
    return {"count": len(rows),
            "episodes": [{k: e[k] for k in
                          ("slug", "title", "dek", "show", "published",
                           "cat", "minutes", "url")}
                         for e in rows[:max(1, min(limit, 50))]]}


TOOLS = [
    {"name": "search_episodes",
     "description": "Search the archive by keyword, show or category. Returns "
                    "matching episodes with title, summary, show and link.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "words that must all appear"},
         "show": {"type": "string", "description": "restrict to one show"},
         "cat": {"type": "string",
                 "enum": ["ai", "biz", "ideas", "hist", "sci", "parent"]},
         "limit": {"type": "integer", "default": 10}}},
     "fn": t_search},
    {"name": "get_episode",
     "description": "Full structured read of one episode: argued points, verbatim "
                    "quotes and numbers, each with a timestamp into the original "
                    "audio. Use the slug from search_episodes.",
     "inputSchema": {"type": "object",
                     "properties": {"slug": {"type": "string"}},
                     "required": ["slug"]},
     "fn": t_get_episode},
    {"name": "list_shows",
     "description": "Every podcast covered, with how many episodes have been "
                    "deep-read and when the latest one landed.",
     "inputSchema": {"type": "object", "properties": {
         "cat": {"type": "string",
                 "enum": ["ai", "biz", "ideas", "hist", "sci", "parent"]}}},
     "fn": t_list_shows},
    {"name": "latest_episodes",
     "description": "Most recent deep-reads, optionally in one category.",
     "inputSchema": {"type": "object", "properties": {
         "cat": {"type": "string",
                 "enum": ["ai", "biz", "ideas", "hist", "sci", "parent"]},
         "limit": {"type": "integer", "default": 10}}},
     "fn": t_latest},
]


# ------------------------------------------------------------------ 协议

def _result(rid, payload):
    return {"jsonrpc": "2.0", "id": rid, "result": payload}


def handle(msg: dict):
    m, rid = msg.get("method"), msg.get("id")
    if m == "initialize":
        return _result(rid, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "ourword", "version": "1.0.0"}})
    if m in ("notifications/initialized", "notifications/cancelled"):
        return None                        # 通知没有 id，不能回 —— 回了对面会报错
    if m == "tools/list":
        return _result(rid, {"tools": [{k: t[k] for k in
                                        ("name", "description", "inputSchema")}
                                       for t in TOOLS]})
    if m == "tools/call":
        p = msg.get("params") or {}
        t = next((x for x in TOOLS if x["name"] == p.get("name")), None)
        if not t:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": f"no such tool: {p.get('name')}"}}
        try:
            out = t["fn"](**(p.get("arguments") or {}))
            text = json.dumps(out, ensure_ascii=False, indent=1)
            return _result(rid, {"content": [{"type": "text", "text": text}]})
        except Exception as ex:
            # 报成工具级错误而不是协议错误：对面能把原因转给用户，
            # 而协议错误多半只会被当成「这个服务器坏了」。
            return _result(rid, {"isError": True, "content": [
                {"type": "text", "text": f"{type(ex).__name__}: {str(ex)[:300]}"}]})
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"unknown method: {m}"}}


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        out = handle(msg)
        if out is not None:
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

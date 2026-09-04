#!/usr/bin/env python3
"""Ping IndexNow after a build so Bing / Yandex / others recrawl new URLs.

Google retired the sitemap ping; IndexNow is what still accepts a push.
The key file must be served at keyLocation (committed next to the site root).
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from build import SITE, ep_url, load  # noqa: E402
from lib.util import log  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
KEY = os.environ.get("INDEXNOW_KEY", "8f3c2a1b9d4e6f708192a3b4c5d6e7f0")
KEY_FILE = ROOT / f"{KEY}.txt"
ENDPOINT = "https://api.indexnow.org/indexnow"


def key_file() -> pathlib.Path:
    KEY_FILE.write_text(KEY + "\n")
    return KEY_FILE


def _trees() -> list[str]:
    """现在真的存在哪几棵树。加第四棵不需要改这里。"""
    root = pathlib.Path(__file__).resolve().parent.parent
    return [""] + [f"/{d}" for d in ("tw", "en")
                   if (root / d / "index.html").exists()]


def urls_to_submit(limit: int = 40) -> list[str]:
    """**三棵树的 URL 都要提交。**

    原来这里只提交简体：`SITE` 是简体的站点根，`ep_url()` 也走简体的 BASE。
    于是 /en/ 和 /tw/ 的新页面从来没被推给搜索引擎——它们只能等爬虫自己
    回来，而那是几天到几周。三棵树同一次推送上线，通知也该是三份。
    """
    eps, _ = load()
    out = []
    for t in _trees():
        base = SITE + t
        out += [base + "/", base + "/sitemap.xml", base + "/llms.txt",
                base + "/sources/"]
    for ep in eps[:limit]:
        try:
            u = ep_url(ep)
        except Exception:
            continue
        for t in _trees():
            # ep_url 给的是简体地址；换成这棵树的前缀
            out.append(u.replace(SITE + "/", SITE + t + "/", 1) if t else u)
    # unique, stable order
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def ping(urls: list[str] | None = None) -> int:
    key_file()
    urls = urls or urls_to_submit()
    if not urls:
        log("indexnow: nothing to submit")
        return 0
    body = {
        "host": "ourword.ai",
        "key": KEY,
        "keyLocation": f"{SITE}/{KEY}.txt",
        "urlList": urls,
    }
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            log(f"indexnow: {r.status} submitted {len(urls)} urls")
            return 0
    except Exception as ex:
        log(f"indexnow: {type(ex).__name__}: {ex}")
        return 0  # never fail the digest over a ping


if __name__ == "__main__":
    raise SystemExit(ping())

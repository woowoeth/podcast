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


def urls_to_submit(limit: int = 40) -> list[str]:
    eps, _ = load()
    out = [SITE + "/", SITE + "/sitemap.xml", SITE + "/llms.txt", SITE + "/sources/"]
    for ep in eps[:limit]:
        try:
            out.append(ep_url(ep))
        except Exception:
            continue
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

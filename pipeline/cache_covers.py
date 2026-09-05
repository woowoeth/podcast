#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把节目封面缓存到本站，600×600 JPEG。

    python3 pipeline/cache_covers.py [--force]

为什么要缓存：og:image 原来直接指向各家播客的 CDN（bbci.co.uk、
megaphone.imgix.net、omnycontent.com…）。分享到微信之后卡片上是一块灰色
占位图 —— 抓图的一方在墙内，这些域名要么慢要么不通；就算通，给的也是
3000×3000、1MB 以上的原图，而我们在 og 里声明的是 600×600，尺寸对不上、
体积又大，抓取超时的概率很高。

_OG_RESIZE 那张表只覆盖两个 host（xyzcdn、imgix），其余原样放行 —— 用户
截图里那一集正好是 megaphone.imgix.net 的，加了参数仍然是 950KB。

缓存之后图从 ourword.ai 自己发出去，尺寸和体积都由我们说了算。
每张约 40KB，一百多个节目共几 MB，跟仓库里其它静态资产一个量级。

manifest 写在 data/covers.json：原始地址 → 本地文件名。build.py 的
og_image() 查这张表，查得到就用本地的，查不到退回原地址（新节目在下一次
缓存之前仍然有图，只是可能抓不到 —— 不因为缓存没跑而变成没有图）。
"""
import hashlib
import io as _io
import json
import os
import sys
import ssl
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets", "cover")
MANIFEST = os.path.join(ROOT, "data", "covers.json")
SIZE = 600
UA = {"User-Agent": "Mozilla/5.0 (compatible; ourword-cover-cache/1.0)"}

# 取图先走 urllib，失败退回 curl。
#
# 这台机器上 192 张有 149 张报 CERTIFICATE_VERIFY_FAILED，理由是
# 「self-signed certificate in certificate chain」—— 网络上有一层 TLS
# 拦截代理，它的根证书在系统钥匙串里（所以 curl 通），不在 certifi 里
# （所以 Python 不通）。这是**本机环境**的事，不是对方 CDN 的事：
# 换 certifi 一张都没多下来。curl 用系统信任库，退回它就绕过去了。
try:
    import certifi
    _CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _CTX = ssl.create_default_context()


def _fetch(url):
    req = urllib.request.Request(url, headers=UA)
    try:
        return urllib.request.urlopen(req, timeout=30, context=_CTX).read()
    except Exception:
        import subprocess
        r = subprocess.run(["curl", "-sSL", "--max-time", "30",
                            "-A", UA["User-Agent"], url],
                           capture_output=True)
        if r.returncode or not r.stdout:
            raise RuntimeError((r.stderr or b"").decode()[:80] or "curl empty")
        return r.stdout


def _urls():
    """所有集子和节目里出现过的封面地址。"""
    seen = []
    d = os.path.join(ROOT, "data", "episodes")
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not f.endswith(".json"):
            continue
        try:
            ep = json.load(open(os.path.join(d, f), encoding="utf-8"))
        except Exception:
            continue
        for u in (ep.get("image"), (ep.get("show") or {}).get("image")):
            if u and u not in seen:
                seen.append(u)
    return seen


def norm(url):
    """归一化成 manifest 的键：去协议、去查询串。

    同一张封面在不同集子里可能带不同的查询串（?aid=rss_feed、
    ?ixlib=...），http/https 也混着。不归一化的话查表查不中 ——
    一千多个页面就那么继续指向外站，而缓存明明下好了。
    """
    u = url.split("?", 1)[0]
    return u.replace("https://", "", 1).replace("http://", "", 1)


def _key(url):
    return hashlib.sha1(norm(url).encode("utf-8")).hexdigest()[:16] + ".jpg"


def main():
    from PIL import Image

    force = "--force" in sys.argv
    os.makedirs(OUT, exist_ok=True)
    man = {}
    if os.path.exists(MANIFEST):
        man = json.load(open(MANIFEST, encoding="utf-8"))

    urls = _urls()
    got = fail = skip = 0
    for u in urls:
        name = _key(u)
        path = os.path.join(OUT, name)
        if not force and os.path.exists(path):
            man[norm(u)] = name
            skip += 1
            continue
        src = u.replace("http://", "https://", 1)
        try:
            raw = _fetch(src)
            im = Image.open(_io.BytesIO(raw)).convert("RGB")
            w, h = im.size
            s = min(w, h)
            im = im.crop(((w - s) // 2, (h - s) // 2,
                          (w - s) // 2 + s, (h - s) // 2 + s))
            im = im.resize((SIZE, SIZE), Image.LANCZOS)
            im.save(path, "JPEG", quality=82, optimize=True, progressive=True)
            man[norm(u)] = name
            got += 1
        except Exception as e:
            # 抓不到也要记一笔（值为 null）。不记的话 og_image 查不到就把原
            # 地址交出去 —— 而那个地址在源站已经 404（microbe.tv 那张），
            # 分享卡上还是一块空白。记下来就能退到站点默认图。
            man[norm(u)] = None
            fail += 1
            print("  ✗ %s  %s" % (u[:70], str(e)[:50]))

    # 清掉不在 manifest 里的孤儿文件。键的算法改过一次（改成归一化之后
    # 哈希全变了），旧的 191 张就那么留在目录里 —— 目录翻倍到 20MB，
    # 而多出来的那一半没有任何页面引用。
    keep = {v for v in man.values() if v}
    gone = 0
    for f in os.listdir(OUT):
        if f.endswith(".jpg") and f not in keep:
            os.remove(os.path.join(OUT, f))
            gone += 1

    json.dump(man, open(MANIFEST, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, sort_keys=True)
    total = sum(os.path.getsize(os.path.join(OUT, f))
                for f in os.listdir(OUT) if f.endswith(".jpg"))
    print("封面缓存：新下 %d，已有 %d，失败 %d，清掉孤儿 %d；共 %d 张 %.1f MB"
          % (got, skip, fail, gone, len(os.listdir(OUT)), total / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())

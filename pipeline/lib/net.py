"""HTTP with a trust store that works both on a MITM'd laptop and on CI.

The laptop this was built on runs a TLS-intercepting proxy: curl trusts its
self-signed root (macOS keychain) but Python's bundled store does not. So the
order is: OS trust store (truststore) -> certifi -> stdlib default. CI hits the
last branch and is happy; the laptop needs the first.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import pathlib
import ssl
import time
import urllib.error
import urllib.request
import zlib

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Substack 对机房 IP 上的浏览器 UA 一律 403，而对播客客户端／抓取器通常放行。
# 403 时依次换这几个再试——静默丢掉 Latent Space 和 Interconnects 这两个
# 自带官方逐字稿的信源，代价太大。
UA_FALLBACKS = (
    "PodcastIndex/1.0 (+https://podcastindex.org)",
    "Overcast/1.0 Podcasts/1.0",
    "Mozilla/5.0 (compatible; Feedly/1.0; +http://www.feedly.com/fetcher.html)",
    "curl/8.7.1",
)

_CTX: ssl.SSLContext | None = None


def ctx() -> ssl.SSLContext:
    global _CTX
    if _CTX is not None:
        return _CTX
    try:                                    # OS trust store — covers proxy roots
        import truststore
        _CTX = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        return _CTX
    except Exception:
        pass
    try:
        import certifi
        _CTX = ssl.create_default_context(cafile=certifi.where())
        return _CTX
    except Exception:
        pass
    _CTX = ssl.create_default_context()
    return _CTX


CACHE = pathlib.Path(os.environ.get("PODCAST_CACHE", ".cache"))


def _decode(resp, raw: bytes) -> bytes:
    enc = (resp.headers.get("Content-Encoding") or "").lower()
    if "gzip" in enc:
        return gzip.decompress(raw)
    if "deflate" in enc:
        try:
            return zlib.decompress(raw)
        except zlib.error:
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw


def get(url: str, *, timeout: int = 45, tries: int = 3, headers: dict | None = None,
        cache_ttl: int = 0, accept: str = "*/*") -> bytes:
    """GET with retry/backoff and an optional on-disk cache.

    cache_ttl > 0 keeps the body under .cache/ for that many seconds. Feeds use
    it so a re-run inside the same hour does not re-hammer every host.
    """
    key = hashlib.sha256(url.encode()).hexdigest()[:24]
    cf = CACHE / (key + ".bin")
    if cache_ttl and cf.exists() and time.time() - cf.stat().st_mtime < cache_ttl:
        return cf.read_bytes()

    h = {"User-Agent": UA, "Accept": accept, "Accept-Encoding": "gzip, deflate",
         "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8"}
    if headers:
        h.update(headers)
    last: Exception | None = None
    tried_uas = False
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout, context=ctx()) as r:
                body = _decode(r, r.read())
            if cache_ttl:
                CACHE.mkdir(parents=True, exist_ok=True)
                cf.write_bytes(body)
            return body
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 403 and not tried_uas:
                tried_uas = True
                for alt in UA_FALLBACKS:
                    try:
                        req = urllib.request.Request(url, headers={**h, "User-Agent": alt})
                        with urllib.request.urlopen(req, timeout=timeout, context=ctx()) as r:
                            body = _decode(r, r.read())
                        if cache_ttl:
                            CACHE.mkdir(parents=True, exist_ok=True)
                            cf.write_bytes(body)
                        return body
                    except Exception:
                        continue
                raise
            if e.code in (403, 404, 401, 410):      # not worth retrying
                raise
            time.sleep(1.5 * (2 ** i))
        except Exception as e:
            last = e
            time.sleep(1.5 * (2 ** i))
    raise last if last else RuntimeError("unreachable")


def get_text(url: str, **kw) -> str:
    return get(url, **kw).decode("utf-8", "replace")


def get_json(url: str, **kw) -> object:
    return json.loads(get(url, accept="application/json", **kw))


def post_json(url: str, payload: dict, headers: dict, *, timeout: int = 300,
              tries: int = 4) -> dict:
    data = json.dumps(payload).encode()
    h = {"Content-Type": "application/json", "User-Agent": UA}
    h.update(headers)
    last: Exception | None = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=h, method="POST")
            with urllib.request.urlopen(req, timeout=timeout, context=ctx()) as r:
                return json.loads(_decode(r, r.read()))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            last = RuntimeError(f"HTTP {e.code}: {detail}")
            if e.code in (400, 401, 403, 404):
                raise last
            time.sleep(3 * (2 ** i))          # 429 / 5xx / overloaded
        except Exception as e:
            last = e
            time.sleep(3 * (2 ** i))
    raise last if last else RuntimeError("unreachable")


def post_multipart(url: str, fields: dict, filename: str, filedata: bytes,
                   headers: dict, *, timeout: int = 900, tries: int = 3) -> dict:
    """Minimal multipart/form-data POST — for Whisper-style audio endpoints."""
    boundary = "----podcast" + hashlib.sha1(filename.encode()).hexdigest()[:16]
    buf = io.BytesIO()
    for k, v in fields.items():
        buf.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    buf.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
              f"filename=\"{filename}\"\r\nContent-Type: audio/mpeg\r\n\r\n".encode())
    buf.write(filedata)
    buf.write(f"\r\n--{boundary}--\r\n".encode())
    body = buf.getvalue()
    h = {"Content-Type": f"multipart/form-data; boundary={boundary}",
         "Content-Length": str(len(body)), "User-Agent": UA}
    h.update(headers)
    last: Exception | None = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers=h, method="POST")
            with urllib.request.urlopen(req, timeout=timeout, context=ctx()) as r:
                return json.loads(_decode(r, r.read()))
        except urllib.error.HTTPError as e:
            msg = ""
            try:
                msg = e.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            last = RuntimeError(f"HTTP {e.code}: {msg}")
            if e.code in (400, 401, 403, 413):
                raise last
            time.sleep(5 * (2 ** i))
        except Exception as e:
            last = e
            time.sleep(5 * (2 ** i))
    raise last if last else RuntimeError("unreachable")

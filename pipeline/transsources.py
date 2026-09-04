#!/usr/bin/env python3
"""把信源简介译成英文，存 data/en/_sources.json。

节目简介是静态配置（resolve_sources.py 里的 desc），一共一百多条，一次调用译完。
不放进 translate.py：那个是按集跑的、带断点，简介是整体一份。
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib import llm                                              # noqa: E402
from lib.util import log                                         # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "en" / "_sources.json"

SYSTEM = ("You translate one-line podcast descriptions from Chinese to English "
          "for a curated podcast site. Keep each one a single line, same length "
          "or shorter, same claim, no marketing language. Show names, hosts and "
          "companies are already in Latin script — copy them byte-identical. "
          "Output valid JSON only.")


def main() -> int:
    srcs = json.loads((ROOT / "data" / "sources.json").read_text())["sources"]
    have = {}
    if OUT.exists():
        try:
            have = json.loads(OUT.read_text()).get("sources", {})
        except Exception:
            have = {}
    todo = [s for s in srcs if s.get("desc") and s["id"] not in have]
    if not todo:
        log("信源简介都译过了")
        return 0
    log(f"要译 {len(todo)} 条（已有 {len(have)} 条）")
    # 分批，一批 40 条：一次全塞进去输出会被 max_tokens 截断
    for i in range(0, len(todo), 40):
        batch = todo[i:i + 40]
        body = "\n".join(f'{s["id"]}: {s["desc"]}' for s in batch)
        ask = ('Translate each line. Return JSON: {"items": [{"id": "...", '
               '"desc": "..."}, ...]} with one entry per input line, same ids.')
        try:
            r = llm.call_json(SYSTEM, body + "\n\n" + ask,
                              max_tokens=4000, temperature=0.2, role="review")
        except Exception as ex:
            log(f"  这一批失败：{type(ex).__name__}")
            continue
        got = 0
        for it in (r.get("items") or []):
            sid, desc = it.get("id"), (it.get("desc") or "").strip()
            if sid and desc:
                have[sid] = {"desc": desc}
                got += 1
        log(f"  第 {i//40 + 1} 批：{got}/{len(batch)} 条")
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps({"sources": have}, ensure_ascii=False,
                                  indent=1) + "\n")
    log(f"共 {len(have)} 条 → {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

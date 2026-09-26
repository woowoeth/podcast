#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""站点默认分享图：assets/og-default.jpg 和 og-default-en.jpg，600×600。

什么时候用它：
- 节目页（/s/<id>/）本来就没有 og:image —— 318 个页面，分享出去全是空白占位；
- 少数集子的封面地址在源站已经 404（microbe.tv 那张），缓存不下来。

一张兜底图不如每集自己的封面，但比一块灰色占位强得多：至少分享卡上有
颜色、有站名，看得出是谁发的。

**卡上写的是这个站自己的名字**，中文「原声」、英文「Podcast」。第一版两张
都写「OurWord.」—— 那是主站（ourword.ai）的名字，写在播客站的分享卡上，
读者转出去之后分不清点进来的是哪个站，而分享卡的整个作用就是让人认出来。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets", "og-default.jpg")
OUT_EN = os.path.join(ROOT, "assets", "og-default-en.jpg")
# 繁体树的分享卡写「原聲」：繁体页的 og:site_name 是原聲，卡上写简体就对不上
OUT_TW = os.path.join(ROOT, "assets", "og-default-tw.jpg")
BG = (26, 25, 23)          # #1a1917 站里的墨色
FG = (244, 242, 236)       # #f4f2ec 纸色
ACC = (226, 118, 78)       # #e2764e 站里的橙红
SIZE = 600


def _font(px, bold=False, index=0):
    from PIL import ImageFont
    # 中文标题要用能画汉字的字体；Georgia 里没有汉字，画出来是空白方块。
    for p in ("/System/Library/Fonts/Songti.ttc",
              "/System/Library/Fonts/Supplemental/Songti.ttc",
              "/System/Library/Fonts/Supplemental/Playfair Display.ttc",
              "/System/Library/Fonts/Supplemental/Georgia Bold.ttf" if bold
              else "/System/Library/Fonts/Supplemental/Georgia.ttf",
              "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf",
              "/Library/Fonts/Arial Bold.ttf"):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, px, index=index if p.endswith(".ttc") else 0)
            except Exception:
                pass
    return ImageFont.load_default()


def _card(path, title, sub, face=0):
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, SIZE - 1, 10], fill=ACC)          # 站里的强调色
    f1 = _font(96 if len(title) <= 3 else 78, True, face)
    # **字体里没有的字会被静默画成空白。** 繁体卡第一版用了 Songti.ttc 的第 0 个
    # 字面（Songti SC Black），它没有「聲」—— 图上只有一个「原」字，脚本照样成功。
    missing = [ch for ch in title if not ch.isspace() and f1.getmask(ch).getbbox() is None]
    if missing:
        raise SystemExit(f"{os.path.basename(path)}：字体画不出 {''.join(missing)}，换一个字面")
    f2 = _font(26)
    w1 = d.textbbox((0, 0), title, font=f1)[2]
    w2 = d.textbbox((0, 0), sub, font=f2)[2]
    # 副标题按标题的实际高度往下让，别写死 —— 汉字标题和拉丁标题的
    # 字身高度差一截，写死的话一边贴着一边悬空。
    top = 214
    h1 = d.textbbox((0, 0), title, font=f1)[3]
    d.text(((SIZE - w1) // 2, top), title, font=f1, fill=FG)
    d.text(((SIZE - w2) // 2, top + h1 + 26), sub, font=f2,
           fill=(141, 134, 123))
    im.save(path, "JPEG", quality=88, optimize=True)
    return os.path.getsize(path)


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    n1 = _card(OUT, "\u539f\u58f0", "ourword.ai")
    n2 = _card(OUT_EN, "Podcast", "ourword.ai")
    _card(OUT_TW, "\u539f\u8072", "ourword.ai", face=2)   # Songti.ttc #2 = Songti TC Bold
    print("\u9ed8\u8ba4\u5206\u4eab\u56fe\uff1a%s %d B\uff0c%s %d B"
          % (os.path.basename(OUT), n1, os.path.basename(OUT_EN), n2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

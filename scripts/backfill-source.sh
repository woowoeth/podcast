#!/usr/bin/env bash
# 把一档源的存量补齐 —— 短的先做，分批跑，留空档给定时线。
#
# **为什么不能一口气跑完。** run.py 有跨进程锁（data/.run.lock），而且是
# 「拿不到就退出」不是排队。补张小珺 142 集要约 19 小时 GPU，一直占着锁的话
# launchd 那两班日更会连着几十次直接跳过，站上就停更了。
# 所以每批之后歇 GAP 秒，让定时线插得进来。
#
# **为什么按时长逐级放开。** GPU 榨不动了（实测：并行转写更慢；
# q4 量化模型 33.0s 比现用的 20.9s 还慢；YouTube 搜到了视频但没有字幕轨）。
# 唯一还能改的是顺序。实测张小珺待做的 142 集：
#     ≤60min   25 集 · 1.7 小时 GPU · 每小时 GPU 出 15.0 篇
#     ≤120min 107 集 · 10.7 小时 GPU · 每小时 GPU 出 10.0 篇
#     不限    142 集 · 19.1 小时 GPU · 每小时 GPU 出  7.4 篇
# 最后那 14 集长访谈吃掉 4.3 小时。先短后长，前两小时就能看见二十多篇上站。
# --max-minutes 在**选集时**就拦，和 --max-words 不同，不会先花掉转写再跳过。
#
# 断点续跑靠两样现成的东西：已发的集不会再被挑中（state.json 的 done），
# 转写按 5 分钟一片落盘，被杀最多损失一片。
#
#   scripts/backfill-source.sh zhangxiaojun
set -u
SRC="${1:?用法：backfill-source.sh <source-id>}"
BATCH="${BATCH:-8}"
GAP="${GAP:-150}"
DAYS="${DAYS:-1700}"
STEPS="${STEPS:-60 90 120 180 0}"
cd "$(dirname "$0")/.."
set -a; . ~/.config/podcast/env 2>/dev/null; set +a
export JOBS="${JOBS:-4}"

total=0
for cap in $STEPS; do
  label=$([ "$cap" = 0 ] && echo "不限时长" || echo "≤${cap} 分钟")
  echo "########## $(date -u +%H:%M:%SZ) 这一档：$label ##########"
  while :; do
    echo "----- $(date -u +%H:%M:%SZ) 批次开始（累计 $total 篇）-----"
    # **边跑边写，不要收进变量。** 上一版用 out=$(...)，整批跑完才落盘，
    # 于是一小时里日志只有 51 字节，问「跑到哪了」只能去数文件。
    before=$(ls data/episodes/*.json 2>/dev/null | wc -l | tr -d ' ')
    python3 pipeline/run.py --only "$SRC" --days "$DAYS" \
        --limit "$BATCH" --per-source "$BATCH" --max-words 0 \
        --max-minutes "$cap" --spend-subscription --no-build 2>&1 \
      | grep -avE "frames/s|^ *[0-9]+%|it/s"
    after=$(ls data/episodes/*.json 2>/dev/null | wc -l | tr -d ' ')
    n=$((after - before))
    # **一集都没出就换下一档。** 可能这一档做完了、锁被占、或剩下的都判掉了 ——
    # 继续循环只是空转抢锁。
    if [ "$n" -le 0 ]; then
      echo "----- $label 这一档没有更多产出，进入下一档 -----"
      break
    fi
    total=$((total + n))
    echo "----- 本批 $n 篇，累计 $total 篇，歇 ${GAP}s 让定时线插队 -----"
    sleep "$GAP"
  done
done
echo "########## 补齐结束：$SRC 本次共 $total 篇 ##########"

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
# **先把自己复制一份再跑。** bash 是边读边执行的；补存量一跑十几个钟头，中间日更会 git pull，
# 要是正好改了这个文件，正在跑的这一份会从半截读到新内容、做出说不清的事。
if [ -z "${BACKFILL_COPY:-}" ]; then
  export BACKFILL_REPO="$(cd "$(dirname "$0")/.." && pwd)"
  # 模板里要有 X：GNU mktemp（CI 的 Linux）不认 BSD 的 `mktemp -t 前缀`，直接报错
  _copy="$(mktemp "${TMPDIR:-/tmp}/backfill-source.XXXXXX")"
  cp "$0" "$_copy"
  BACKFILL_COPY=1 exec bash "$_copy" "$@"
fi
SRC="${1:?用法：backfill-source.sh <source-id>}"
BATCH="${BATCH:-8}"
GAP="${GAP:-150}"
DAYS="${DAYS:-1700}"
STEPS="${STEPS:-60 90 120 180 0}"
cd "${BACKFILL_REPO:?}"
set -a; . ~/.config/podcast/env 2>/dev/null; set +a
export JOBS="${JOBS:-4}"

# 没有 API key 时和本机日更（local-daily.sh）用同一套省 token 的模型。不设的话 CLI 后端每个角色
# 都落到默认的 opus：补罗永浩 36 集（124 小时音频）全程 opus，比省 token 的配置贵几倍
# （2026-10-09 用户要补全集时查出）。守护 BackfillUsesTheLeanModels 盯着两边一致。
if [ -z "${LLM_API_KEY:-}" ]; then
  : "${LLM_MODEL:=sonnet}"
  : "${LLM_MODEL_DIGEST:=sonnet}"
  : "${LLM_MODEL_TRIAGE:=haiku}"
  : "${LLM_MODEL_MAP:=haiku}"
  : "${LLM_MODEL_REVIEW:=haiku}"
  export LLM_MODEL LLM_MODEL_DIGEST LLM_MODEL_TRIAGE LLM_MODEL_MAP LLM_MODEL_REVIEW
fi

# **定时线前后让出来。** 跑批锁是「拿不到就退出」：补跑正占着锁时 launchd 那一班日更整轮跳过，
# 补出来的稿也就没人提交上线（发布靠日更那一班的 git add + 建站 + 推送）。只歇 GAP 秒是碰运气 ——
# 一批长集要一个钟头。所以：日更前后的窗口里不开新批，日更在跑的时候也不开。
# 窗口按本机时间，覆盖 launchd 的 10:30 / 21:30 加上一批最长的耗时。
# 默认不再设窗口：日更开头会先等这边这一批跑完（pipeline/runlock.py），这边看到日更在跑就不开新批。
# 想手动留空档（比如白天要用 GPU）就设 QUIET="09:30-11:45 …"。
QUIET="${QUIET:-}"
in_quiet() {
  local now a b w
  now=$((10#$(date +%H%M)))
  for w in $QUIET; do
    a=${w%-*}; b=${w#*-}
    a=$((10#${a/:/})); b=$((10#${b/:/}))
    [ "$now" -ge "$a" ] && [ "$now" -lt "$b" ] && return 0
  done
  return 1
}
wait_turn() {
  local said=""
  # 只认真正在跑的日更（launchd 起的是 /bin/bash …/scripts/local-daily.sh）。原来 pgrep -f 只要命令行里
  # 出现这串字就算 —— 一个 grep、一个监控循环、一条带着这个路径的命令都会让补存量无限期等下去。
  while in_quiet || pgrep -f '^(/bin/)?bash .*scripts/local-daily\.sh( |$)' >/dev/null; do
    [ -z "$said" ] && echo "----- $(date +%H:%M) 日更的窗口／日更在跑，先让出来 -----" && said=1
    sleep 120
  done
}

total=0
for cap in $STEPS; do
  label=$([ "$cap" = 0 ] && echo "不限时长" || echo "≤${cap} 分钟")
  echo "########## $(date -u +%H:%M:%SZ) 这一档：$label ##########"
  while :; do
    wait_turn
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

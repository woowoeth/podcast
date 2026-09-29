#!/bin/bash
# 云端 bot 的推送都经过这里，好让推上去的提交也过一遍 ci.yml。
#
# 为什么需要它：用 GITHUB_TOKEN 推上去的提交**不启动任何别的工作流**（GitHub 的规则），
# 所以 bot 的推送从来没跑过 CI。2026-09-28 curate 推出去半套站，ci.yml「产物和仓库一致」
# 那一步本来能抓住，可它根本没跑（POSTMORTEM 34）。例外是 workflow_dispatch（和
# repository_dispatch）：GITHUB_TOKEN 派的单会真的起一个 run。
#
# 两个子命令：
#   bash scripts/bot-push.sh git push [参数…]
#       原样执行这条 git push，退出码原样返回；推成功了就记下 HEAD，
#       作为「这一轮推上去的最后一个提交」。写成 `bot-push.sh git push` 而不是换个名字，
#       是为了让「git push」这几个字还在 —— 守护里查重试循环、查心跳推送的那些规则都认它。
#   bash scripts/bot-push.sh ci
#       job 的最后一步（if: always()）：这一轮推过就给 ci.yml 派一次单，查记下的那个提交。
#       没推过就什么都不做。派不出去就红 —— 推上去了却没人查，不能是绿的。
#
# **只查最后一个，不是每推一次查一次。** 日更先推一个只有数据的提交、再推重建的站：
# 中间那个提交产物本来就是旧的，逐个查就是每天必红的误报。最后一个才是这一轮留给 main 的
# 样子 —— 重建那步失败、只剩数据提交的时候，它就是最后一个，CI 照样会红，这正是要抓的。
set -uo pipefail

LOG="${BOT_PUSH_LOG:-${RUNNER_TEMP:-/tmp}/bot-pushed-sha}"

case "${1:-}" in
  git)
    [ "${2:-}" = push ] || { echo "::error::bot-push.sh 只包 git push，收到的是：$*" >&2; exit 2; }
    # 记的是 HEAD，所以只认推 HEAD 的写法（git push / git push origin HEAD:main）。
    # 推别的引用时记下的就不是推上去的那个提交，宁可当场拒绝。
    shift 2
    pos=0
    for a in "$@"; do
      case "$a" in -*) continue;; esac
      pos=$((pos + 1))
      [ "$pos" = 1 ] && continue          # 远端名
      case "$a" in
        HEAD|HEAD:*) ;;
        *) echo "::error::bot-push.sh 只认推 HEAD 的写法，收到 refspec「${a}」" >&2; exit 2;;
      esac
    done
    git push "$@"
    rc=$?
    [ "$rc" = 0 ] || exit "$rc"
    git rev-parse HEAD > "$LOG" || {
      echo "::error::推上去了，却没记下是哪个提交（${LOG}）—— CI 不会查它" >&2; exit 1; }
    exit 0
    ;;
  ci)
    if [ ! -s "$LOG" ]; then
      echo "这一轮没有推送，不派 CI"
      exit 0
    fi
    sha=$(cat "$LOG")
    ref="${GITHUB_REF_NAME:-main}"
    from="${GITHUB_WORKFLOW:-?} #${GITHUB_RUN_ID:-?}"
    for i in 1 2 3; do
      if gh workflow run ci.yml --ref "$ref" -f sha="$sha" -f from="$from"; then
        echo "::notice::已派 ci.yml 查 ${sha}（${ref}，来自 ${from}）"
        exit 0
      fi
      echo "派单失败，第 $i 次"; sleep $((i * ${BOT_PUSH_BACKOFF:-5}))
    done
    echo "::error::推上去的 $sha 没能派出 CI —— 这个提交没人查过" >&2
    exit 1
    ;;
  *)
    echo "用法：bot-push.sh git push [参数…] | bot-push.sh ci" >&2
    exit 2
    ;;
esac

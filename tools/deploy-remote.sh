#!/bin/bash
# Deploy zcharlie to the production host (ai-server since 2026-10-15).
#
#   tools/deploy-remote.sh [deploy] [--no-backup]   push main to the host, run tools/deploy.sh there
#   tools/deploy-remote.sh status                   tools/deploy.sh status on the host
#   tools/deploy-remote.sh rollback [--force]       tools/deploy.sh rollback on the host
#
# The host's checkout is a plain git repo with receive.denyCurrentBranch
# updateInstead, so `git push` updates its working tree; tools/deploy.sh there
# then builds from that HEAD (see docs/product/operations.md 2.2). Only `main`
# is deployed. Env knobs: DEPLOY_HOST (ssh host, default ai-server),
# DEPLOY_REMOTE (git remote, default ai-server), DEPLOY_DIR (checkout on the
# host, relative to its home, default services/zcharlie).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

HOST="${DEPLOY_HOST:-ai-server}"
REMOTE="${DEPLOY_REMOTE:-ai-server}"
DIR="${DEPLOY_DIR:-services/zcharlie}"

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31mxx\033[0m %s\n' "$*" >&2; exit 1; }

cmd="${1:-deploy}"
[ $# -gt 0 ] && shift

if [ "$cmd" = deploy ]; then
    git remote get-url "$REMOTE" >/dev/null 2>&1 \
        || die "没有 git remote ${REMOTE}：git remote add ${REMOTE} ${HOST}:${DIR}"
    [ "$(git branch --show-current)" = main ] || die "只部署 main 分支（当前 $(git branch --show-current)）"
    if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
        printf '\033[33m!!\033[0m %s\n' "工作区有未提交的改动：只部署已提交的 HEAD $(git rev-parse --short=12 HEAD)" >&2
    fi
    say "推送 main -> ${REMOTE}"
    git push "$REMOTE" main:main
    remote_head="$(ssh "$HOST" "git -C $(printf '%q' "$DIR") rev-parse HEAD")"
    [ "$remote_head" = "$(git rev-parse HEAD)" ] \
        || die "${HOST} 上的 HEAD（${remote_head:0:12}）与本地不一致：检查 ${DIR} 的工作区是否有改动"
fi

case "$cmd" in
    deploy|status|rollback) ;;
    -h|--help|help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "未知命令: $cmd（deploy | status | rollback）" ;;
esac

say "在 ${HOST}:${DIR} 执行 tools/deploy.sh ${cmd} $*"
args="$(printf ' %q' "$cmd" "$@")"
exec ssh -t "$HOST" "cd $(printf '%q' "$DIR") && tools/deploy.sh${args}"

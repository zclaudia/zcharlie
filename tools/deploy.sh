#!/bin/bash
# One-command Docker deployment for zcharlie (api + worker + scheduler).
#
#   tools/deploy.sh [deploy] [--no-backup]   pre-flight -> backup -> build -> migrate -> start -> verify
#   tools/deploy.sh status                   services, health, running version, schema revision
#   tools/deploy.sh rollback [--force]       back to the image of the previous deploy
#
# deploy:
#   1. pre-flight: docker up, .env has the required keys, every external network
#      the compose files reference exists, all services share one DATABASE_URL
#   2. stops worker + scheduler, takes an online SQLite backup with the CURRENT
#      image and verifies it (~/backups/deploy-<time>-<rev>, newest 5 kept);
#      a failed backup restarts them and aborts
#   3. tags the current image investment-agent:prev, builds the new one from
#      `git archive HEAD` (never the working tree) with GIT_SHA baked in
#   4. `docker compose up -d`: the one-shot `migrate` service runs first, then
#      api, then worker + scheduler once the api is healthy
#   5. waits for all three to be healthy and checks /healthz reports the new rev
#   6. `docker image prune -f`: drops dangling images (each deploy leaves the
#      replaced :prev and the builder stage behind); tagged images stay
# rollback re-tags :prev as :dev and restarts api/worker/scheduler WITHOUT the
# migrate step (old code cannot migrate a newer schema). When the failed deploy
# changed the schema it refuses unless --force: restore the backup first
# (docs/product/backup-and-recovery.md).
#
# Production runs on ai-server (since 2026-10-15): deploy from a dev machine
# with tools/deploy-remote.sh. A checkout that is not the production host keeps
# an untracked .deploy/remote (one line: the production host); deploy and
# rollback then refuse to start a second stack against a stale database.
# The marker is machine state and not tracked, so a fresh clone lacks it: on a
# non-Linux machine (production is Linux) deploy/rollback then refuse too,
# with --allow-local as the explicit escape hatch for intentional local runs.
#
# Machine-specific settings (ports, external networks, the DB volume) stay in
# docker-compose.override.yml. Env knobs: BACKUP_ROOT (default ~/backups),
# DEPLOY_WAIT_SECONDS (default 300).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

IMAGE="investment-agent:dev"
PREV_IMAGE="investment-agent:prev"
STATE_DIR="$REPO/.deploy"
STATE_FILE="$STATE_DIR/last.env"
REMOTE_MARKER="$STATE_DIR/remote"
BACKUP_ROOT="${BACKUP_ROOT:-$HOME/backups}"
WAIT_SECONDS="${DEPLOY_WAIT_SECONDS:-300}"
KEEP_BACKUPS=5
REQUIRED_ENV_KEYS=(API_KEY SETTINGS_ENCRYPTION_KEY)
LONG_RUNNING=(api worker scheduler)

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31mxx\033[0m %s\n' "$*" >&2; exit 1; }

refuse_if_remote() {
    [ -f "$REMOTE_MARKER" ] || return 0
    die "生产环境在 $(head -n1 "$REMOTE_MARKER")，本机不部署（${REMOTE_MARKER}）。
   用 tools/deploy-remote.sh ${1:-deploy}；确需在本机运行先删除该文件。"
}

# The remote marker is machine state (deliberately untracked), so a fresh
# clone has no protection against accidentally starting a second stack on the
# dev machine. Production runs on Linux; on any other machine, refuse unless
# --allow-local confirms an intentional local run.
refuse_unconfirmed_local() {
    [ "$(uname -s)" = "Linux" ] && return 0
    [ -f "$REMOTE_MARKER" ] && return 0
    [ "${ALLOW_LOCAL:-0}" = 1 ] && return 0
    die "这台机器没有 .deploy/remote 标记，而生产环境跑在 Linux 主机 ai-server
   （docs/product/operations.md 2.2）。本机部署会另起一套独立的栈（空数据库，
   worker 可能与生产任务重复）。确要本机部署加 --allow-local；这台机器若就是
   生产/带生产卷的主机，恢复标记：mkdir -p .deploy && echo <生产主机名> > .deploy/remote。"
}

image_exists() { docker image inspect "$1" >/dev/null 2>&1; }

image_revision() {
    docker image inspect -f '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$1" 2>/dev/null || true
}

# Reads `docker compose config` (which contains secrets) and prints only
# non-secret facts: "DB <container path>|-", "NET <external network>",
# "MISMATCH <service>" for services whose DATABASE_URL differs from the api's.
compose_facts() {
    docker compose config --format json | python3 -c '
import json, sys
cfg = json.load(sys.stdin)
services = cfg.get("services", {})
urls = {name: (svc.get("environment") or {}).get("DATABASE_URL", "") for name, svc in services.items()}
api = urls.get("api", "")
prefix = "sqlite+aiosqlite:///"
print("DB", api[len(prefix):] if api.startswith(prefix) else "-")
for name, url in sorted(urls.items()):
    if url != api:
        print("MISMATCH", name)
for key, net in (cfg.get("networks") or {}).items():
    if net.get("external"):
        print("NET", net.get("name") or key)
'
}

# Alembic revision of the live database ("none" when it cannot be read).
db_revision() {
    local out
    if docker compose ps --status running --services 2>/dev/null | grep -qx api; then
        out="$(docker compose exec -T api ia migrate current 2>/dev/null || true)"
    elif image_exists "$IMAGE"; then
        out="$(docker compose run --rm --no-deps -T migrate ia migrate current 2>/dev/null || true)"
    fi
    out="$(printf '%s\n' "${out:-}" | grep -Eo '^[0-9A-Za-z_]+( \(head\))?$' | tail -1 | cut -d' ' -f1)"
    printf '%s\n' "${out:-none}"
}

running_version() {
    docker compose exec -T api curl -fsS http://localhost:8000/healthz 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("version",""))' 2>/dev/null || true
}

# Prints "<service> <state> <health>" for every compose service.
service_states() {
    docker compose ps -a --format json | python3 -c '
import json, sys
raw = sys.stdin.read().strip()
rows = json.loads(raw) if raw.startswith("[") else [json.loads(l) for l in raw.splitlines() if l.strip()]
for r in sorted(rows, key=lambda r: r["Service"]):
    print(r["Service"], r.get("State") or "-", r.get("Health") or "-")
'
}

wait_healthy() {
    local deadline=$((SECONDS + WAIT_SECONDS)) pending
    while :; do
        pending=""
        for svc in "${LONG_RUNNING[@]}"; do
            service_states | grep -q "^$svc running healthy$" || pending+=" $svc"
        done
        [ -z "$pending" ] && return 0
        if [ "$SECONDS" -ge "$deadline" ]; then
            warn "超时（${WAIT_SECONDS}s）仍未健康:${pending}"
            return 1
        fi
        sleep 5
    done
}

print_status() {
    printf '\n%-10s %-10s %s\n' SERVICE STATE HEALTH
    service_states | while read -r svc state health; do
        printf '%-10s %-10s %s\n' "$svc" "$state" "$health"
    done
    printf '\n运行版本   %s\n' "$(running_version || true)"
    printf '数据库迁移 %s\n' "$(db_revision)"
    if [ -f "$STATE_FILE" ]; then
        # shellcheck disable=SC1090
        . "$STATE_FILE"
        printf '上次部署   %s  %s -> %s  迁移 %s -> %s\n' \
            "${DEPLOYED_AT:-?}" "${PREVIOUS_VERSION:-?}" "${VERSION:-?}" "${REV_BEFORE:-?}" "${REV_AFTER:-?}"
        [ -n "${BACKUP_DIR:-}" ] && printf '部署前备份 %s\n' "$BACKUP_DIR"
    fi
}

preflight() {
    say "预检"
    docker info >/dev/null 2>&1 || die "docker 未运行（colima 是否已启动？）"
    docker compose version >/dev/null 2>&1 || die "缺少 docker compose v2"
    command -v python3 >/dev/null || die "缺少 python3"
    [ -f .env ] || die ".env 不存在（参考 .env.example）"
    for key in "${REQUIRED_ENV_KEYS[@]}"; do
        grep -Eq "^${key}=.+" .env || die ".env 缺少 ${key}"
    done
    docker compose config --quiet || die "compose 配置无效"
    local facts
    facts="$(compose_facts)" || die "无法解析 compose 配置"
    if grep -q '^MISMATCH ' <<<"$facts"; then
        die "这些服务的 DATABASE_URL 与 api 不一致: $(grep '^MISMATCH ' <<<"$facts" | cut -d' ' -f2 | tr '\n' ' ')（检查 docker-compose.override.yml）"
    fi
    while read -r _ net; do
        [ -n "$net" ] || continue
        docker network inspect "$net" >/dev/null 2>&1 \
            || die "外部网络 ${net} 不存在：先启动它所在的 compose 项目"
    done < <(grep '^NET ' <<<"$facts" || true)
    DB_PATH="$(grep '^DB ' <<<"$facts" | cut -d' ' -f2)"
    GIT_SHA="$(git rev-parse --short=12 HEAD)"
    if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
        warn "工作区有未提交的改动或未跟踪文件：它们不会进入镜像（镜像只从 HEAD ${GIT_SHA} 构建）"
    fi
}

# Builds from `git archive HEAD`, never the working tree, so uncommitted or
# untracked files (including another session's work in progress) never ship.
build_image() {
    local dir status=0 log="$STATE_DIR/build.log" uid_args=()
    # On a Linux host the bind mounts (./data, $BACKUP_ROOT) keep host
    # ownership, so the container runs as the deploying user. colima's
    # virtiofs maps ownership itself: keep the image default (10001) there.
    if [ "$(uname -s)" = "Linux" ]; then
        uid_args=(--build-arg APP_UID="$(id -u)" --build-arg APP_GID="$(id -g)")
    fi
    dir="$(mktemp -d)"
    mkdir -p "$STATE_DIR"
    git archive --format=tar HEAD | tar -x -C "$dir" \
        && docker build -t "$IMAGE" --build-arg GIT_SHA="$GIT_SHA" "${uid_args[@]}" "$dir" >"$log" 2>&1 \
        || status=$?
    rm -rf -- "$dir"
    if [ "$status" -ne 0 ]; then
        tail -n 30 "$log" >&2
        warn "完整构建日志：$log"
    fi
    return "$status"
}

backup() {
    if [ "$DB_PATH" = "-" ]; then
        warn "数据库不是 SQLite，跳过部署前备份"
        return 0
    fi
    if ! image_exists "$IMAGE"; then
        warn "还没有 ${IMAGE} 镜像（首次部署），跳过备份"
        return 0
    fi
    local name="deploy-$(date +%Y%m%d-%H%M%S)-${GIT_SHA}"
    BACKUP_DIR="$BACKUP_ROOT/$name"
    mkdir -p "$BACKUP_ROOT"
    say "备份数据库 -> ${BACKUP_DIR}"
    if ! docker compose run --rm --no-deps -T \
            -v "$REPO/tools:/app/tools:ro" -v "$BACKUP_ROOT:/backups" migrate \
            python tools/backup_research.py --data-dir /app/data --db "$DB_PATH" \
            --output "/backups/$name"; then
        return 1
    fi
    python3 tools/verify_backup.py --backup-dir "$BACKUP_DIR" >/dev/null || return 1
    if command -v zstd >/dev/null; then
        zstd -q -T0 -10 --rm "$BACKUP_DIR/app.db" -o "$BACKUP_DIR/app.db.zst" \
            && zstd -q -t "$BACKUP_DIR/app.db.zst" || return 1
    fi
    ls -1d "$BACKUP_ROOT"/deploy-* 2>/dev/null | sort -r | tail -n +$((KEEP_BACKUPS + 1)) \
        | while read -r old; do rm -rf -- "$old"; done || true
}

restart_previous() {
    warn "恢复原来的 worker / scheduler"
    docker compose start worker scheduler >/dev/null 2>&1 || true
}

deploy() {
    local no_backup=0
    for arg in "$@"; do
        case "$arg" in
            --no-backup) no_backup=1 ;;
            --allow-local) ;;
            *) die "未知参数: $arg" ;;
        esac
    done
    preflight
    local previous_version rev_before rev_after
    previous_version="$(image_revision "$IMAGE")"
    rev_before="$(db_revision)"
    say "部署 ${previous_version:-（无）} -> ${GIT_SHA}（数据库迁移 ${rev_before}）"

    BACKUP_DIR=""
    say "停止 worker / scheduler"
    docker compose stop worker scheduler >/dev/null 2>&1 || true
    if [ "$no_backup" -eq 0 ]; then
        backup || { restart_previous; die "备份失败，已中止部署（未改动镜像和数据库）"; }
    else
        warn "已跳过部署前备份（--no-backup）"
    fi

    if image_exists "$IMAGE"; then
        docker tag "$IMAGE" "$PREV_IMAGE"
    fi
    say "构建镜像 ${IMAGE}（${GIT_SHA}）"
    build_image || { restart_previous; die "构建失败，已中止部署（数据库未改动）"; }

    say "迁移数据库并启动 api / worker / scheduler"
    if ! GIT_SHA="$GIT_SHA" docker compose up -d --no-build; then
        docker compose logs --tail 40 migrate >&2 || true
        die "启动失败。迁移日志见上；部署前备份：${BACKUP_DIR:-无}；回退：tools/deploy.sh rollback"
    fi

    say "等待健康检查（最多 ${WAIT_SECONDS}s）"
    if ! wait_healthy; then
        print_status
        die "服务未就绪：docker compose logs <service> 查看原因；回退：tools/deploy.sh rollback"
    fi
    local live
    live="$(running_version)"
    [ "$live" = "$GIT_SHA" ] || die "api 报告的版本是 '${live}'，不是刚构建的 ${GIT_SHA}"
    rev_after="$(db_revision)"

    mkdir -p "$STATE_DIR"
    cat >"$STATE_FILE" <<EOF
DEPLOYED_AT='$(date '+%Y-%m-%d %H:%M:%S')'
VERSION='${GIT_SHA}'
PREVIOUS_VERSION='${previous_version}'
REV_BEFORE='${rev_before}'
REV_AFTER='${rev_after}'
BACKUP_DIR='${BACKUP_DIR}'
EOF
    say "清理悬空镜像"
    docker image prune -f >/dev/null || warn "清理悬空镜像失败（不影响本次部署）"
    say "部署完成"
    print_status
}

rollback() {
    local force=0
    for arg in "$@"; do
        case "$arg" in
            --force) force=1 ;;
            --allow-local) ;;
            *) die "未知参数: $arg" ;;
        esac
    done
    docker info >/dev/null 2>&1 || die "docker 未运行"
    image_exists "$PREV_IMAGE" || die "没有可回退的镜像 ${PREV_IMAGE}"
    REV_BEFORE="" REV_AFTER="" BACKUP_DIR=""
    # shellcheck disable=SC1090
    [ -f "$STATE_FILE" ] && . "$STATE_FILE"
    if [ -n "$REV_BEFORE" ] && [ "$REV_BEFORE" != "$REV_AFTER" ] && [ "$force" -eq 0 ]; then
        die "上次部署把数据库从 ${REV_BEFORE} 迁移到了 ${REV_AFTER}，旧代码未必兼容。
   先按 docs/product/backup-and-recovery.md 用备份 ${BACKUP_DIR:-（无）} 恢复数据库，
   或确认兼容后加 --force。"
    fi
    local current
    current="$(image_revision "$IMAGE")"
    say "回退 ${current:-?} -> $(image_revision "$PREV_IMAGE")"
    image_exists "$IMAGE" && docker tag "$IMAGE" "investment-agent:rolled-back"
    docker tag "$PREV_IMAGE" "$IMAGE"
    # --no-deps: skip `migrate` — the previous code cannot migrate a newer schema.
    docker compose up -d --no-build --no-deps --force-recreate "${LONG_RUNNING[@]}"
    say "等待健康检查（最多 ${WAIT_SECONDS}s）"
    wait_healthy || { print_status; die "回退后服务未就绪"; }
    say "回退完成（被替换的镜像保存为 investment-agent:rolled-back）"
    print_status
}

cmd="${1:-deploy}"
[ $# -gt 0 ] && shift
case "$cmd" in
    deploy|rollback)
        for arg in "$@"; do [ "$arg" = "--allow-local" ] && ALLOW_LOCAL=1; done
        refuse_if_remote "$cmd"
        refuse_unconfirmed_local
        ;;
esac
case "$cmd" in
    deploy) deploy "$@" ;;
    status) docker info >/dev/null 2>&1 || die "docker 未运行"; print_status ;;
    rollback) rollback "$@" ;;
    -h|--help|help) sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//' ;;
    *) die "未知命令: $cmd（deploy | status | rollback）" ;;
esac

#!/bin/sh
# tests/browser 回归 runner（review P3 测试项：浏览器脚本 runner 化）。
#
# tests/browser/*.mjs 依赖两个本机环境变量并在仓库根目录运行：
#   - AG17_NODE_MODULES：带 playwright 的 node_modules（脚本用 createRequire 解析）
#   - AG17_CHROMIUM_EXECUTABLE：Chromium / chrome-headless-shell 可执行文件
# 过去的复用命令把这两个路径硬编码成具体机器的绝对路径（见 docs/product/ 各卡记录），
# 换机器即失传。本脚本提供带合理默认值的环境变量探测，并从仓库根目录串行运行脚本
# （各脚本使用固定端口，禁止并行）。
#
# 用法：
#   tests/browser/run.sh                     # 串行运行全部 *.mjs
#   tests/browser/run.sh agent_views.mjs ... # 只运行指定脚本
#
# 覆盖探测结果的环境变量：
#   NODE_BIN、AG17_NODE_MODULES、AG17_CHROMIUM_EXECUTABLE

set -eu
cd "$(dirname "$0")/../.."

NODE_BIN="${NODE_BIN:-$(command -v node || true)}"
if [ -z "$NODE_BIN" ]; then
  echo "run.sh: 未找到 node；请设置 NODE_BIN=/path/to/node" >&2
  exit 1
fi

find_node_modules() {
  for candidate in \
    "$HOME"/.cache/codex-runtimes/*/dependencies/node/node_modules \
    "$HOME"/.cache/zcode/*/node_modules \
    "$(pwd)/node_modules"; do
    if [ -d "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

find_chromium() {
  for pattern in \
    "$HOME"/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-mac-arm64/chrome-headless-shell \
    "$HOME"/Library/Caches/ms-playwright/chromium-*/chrome-mac-*/Chromium.app/Contents/MacOS/Chromium \
    "$HOME"/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell \
    "$HOME"/.cache/ms-playwright/chromium-*/chrome-linux*/chrome; do
    for candidate in $pattern; do
      if [ -x "$candidate" ]; then
        printf '%s\n' "$candidate"
        return 0
      fi
    done
  done
  return 1
}

if [ -z "${AG17_NODE_MODULES:-}" ]; then
  AG17_NODE_MODULES=$(find_node_modules || true)
fi
if [ -z "${AG17_CHROMIUM_EXECUTABLE:-}" ]; then
  AG17_CHROMIUM_EXECUTABLE=$(find_chromium || true)
fi
if [ -z "$AG17_NODE_MODULES" ] || [ -z "$AG17_CHROMIUM_EXECUTABLE" ]; then
  echo "run.sh: 未能自动定位 playwright node_modules 或 Chromium；" >&2
  echo "  请设置 AG17_NODE_MODULES=/path/to/node_modules AG17_CHROMIUM_EXECUTABLE=/path/to/chrome" >&2
  exit 1
fi
export AG17_NODE_MODULES AG17_CHROMIUM_EXECUTABLE

echo "run.sh: NODE_BIN=$NODE_BIN"
echo "run.sh: AG17_NODE_MODULES=$AG17_NODE_MODULES"
echo "run.sh: AG17_CHROMIUM_EXECUTABLE=$AG17_CHROMIUM_EXECUTABLE"

if [ "$#" -gt 0 ]; then
  scripts=""
  for name in "$@"; do
    case "$name" in
      */*) scripts="$scripts $name" ;;
      *) scripts="$scripts tests/browser/$name" ;;
    esac
  done
else
  scripts=$(ls tests/browser/*.mjs)
fi

status=0
for script in $scripts; do
  echo "run.sh: $NODE_BIN $script"
  if ! "$NODE_BIN" "$script"; then
    echo "run.sh: $script 失败" >&2
    status=1
  fi
done
exit "$status"

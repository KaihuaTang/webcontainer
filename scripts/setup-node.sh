#!/usr/bin/env bash
# 装一份平台自带的 Node 运行时到 .node/（与 .venv 同理：版本由平台钉住，不看系统 node）。
#
# 只有 Node 子项目需要它：container/TideLoom、container/ZeroHourChoir 这类
# vinext（Next.js on Vite）项目要求 Node >= 22，而系统 node 往往更旧
# （本机 /usr/bin/node 是 20.20.2，跑 vinext 会报
#  "does not provide an export named 'glob'"）。
#
# 用法：
#   ./scripts/setup-node.sh                       # 装默认版本
#   NODE_VERSION=v22.23.2 ./scripts/setup-node.sh # 指定版本
#   https_proxy=http://127.0.0.1:7892 ./scripts/setup-node.sh   # 本机需代理出海
#
# 幂等：已是目标版本则直接跳过。网关会把 .node/bin 前置到子项目进程的 PATH，
# 所以 project.json 里写 "node" / "npx" 就是这一份（见 gateway/supervisor.py）。
set -euo pipefail

cd "$(dirname "$0")/.."
NODE_VERSION="${NODE_VERSION:-v24.18.1}"   # 24.x = Krypton LTS
DEST=".node"

if [[ -x "$DEST/bin/node" && "$("$DEST/bin/node" -v)" == "$NODE_VERSION" ]]; then
    echo "==> $DEST 已是 $NODE_VERSION，跳过"
    exit 0
fi

case "$(uname -m)" in
    x86_64)        ARCH=x64 ;;
    aarch64|arm64) ARCH=arm64 ;;
    *) echo "不支持的架构：$(uname -m)" >&2; exit 1 ;;
esac
TARBALL="node-${NODE_VERSION}-linux-${ARCH}.tar.xz"
BASE_URL="https://nodejs.org/dist/${NODE_VERSION}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "==> 下载 $BASE_URL/$TARBALL"
curl -fsSL -o "$TMP/$TARBALL" "$BASE_URL/$TARBALL"
curl -fsSL -o "$TMP/SHASUMS256.txt" "$BASE_URL/SHASUMS256.txt"
echo "==> 校验 sha256"
(cd "$TMP" && grep " ${TARBALL}\$" SHASUMS256.txt | sha256sum -c -)

rm -rf "$DEST"
mkdir -p "$DEST"
tar -xJf "$TMP/$TARBALL" -C "$DEST" --strip-components=1

echo "==> 完成：node $("$DEST/bin/node" -v) / npm $("$DEST/bin/npm" -v)"
echo "    Node 子项目装依赖： PATH=\"\$PWD/$DEST/bin:\$PATH\" npm install"

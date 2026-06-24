#!/usr/bin/env bash
# 启动 mitmproxy(带浏览器界面),只拦截并 dump 发往 scitix 的流量。
#   - 监听 127.0.0.1:8888,作为 Claude Code 的 HTTPS 代理
#   - 上游链到你已有的 7897 代理(verge-mihomo)
#   - 【关键】--allow-hosts:只对 scitix 做 TLS 中间人(MITM)解密+dump;
#     其它所有流量(pypi/github/digikey/MCP……)以 raw TCP 隧道透传,
#     出示真实证书 → curl/pip/python/node 无需信任 mitmproxy CA,照常工作。
#   - 加载 dump_scitix.py:只 dump scitix 流量到 ./dumps/
#   - mitmweb 网页界面默认 http://127.0.0.1:8081
set -euo pipefail
cd "$(dirname "$0")"

export DUMP_DIR="$(pwd)/dumps"
mkdir -p "$DUMP_DIR"

# 定位 mitmweb:① PATH(brew 安装)→ ② 本仓库的 .venv(Quickstart 用 uv 建的)→ ③ 兜底
MITMWEB="$(command -v mitmweb || true)"
if [ -z "$MITMWEB" ] && [ -x "./.venv/bin/mitmweb" ]; then
  MITMWEB="$(pwd)/.venv/bin/mitmweb"
fi
if [ -z "$MITMWEB" ]; then
  MITMWEB="/Library/Frameworks/Python.framework/Versions/3.13/bin/mitmweb"
fi

UPSTREAM="${UPSTREAM_PROXY:-http://127.0.0.1:7897}"
# 只有匹配该正则的 host 才被 MITM,其余透传。默认只拦 scitix。
ALLOW_HOSTS="${ALLOW_HOSTS:-scitix}"
# 端口可经 env 覆盖(看门狗测试用),默认 8888 / 网页 8081
MITM_PORT="${MITM_PORT:-8888}"
MITM_WEBPORT="${MITM_WEBPORT:-8081}"

echo "[start-mitm] 监听 127.0.0.1:$MITM_PORT  上游 $UPSTREAM"
echo "[start-mitm] 只 MITM 主机: $ALLOW_HOSTS (其余透传)"
echo "[start-mitm] dump 目录 $DUMP_DIR"
echo "[start-mitm] 网页界面 http://127.0.0.1:$MITM_WEBPORT"

exec "$MITMWEB" \
  --mode "upstream:$UPSTREAM" \
  --listen-host 127.0.0.1 --listen-port "$MITM_PORT" \
  --allow-hosts "$ALLOW_HOSTS" \
  --set web_host=127.0.0.1 --set web_port="$MITM_WEBPORT" \
  --set web_open_browser=false \
  --set confdir=~/.mitmproxy \
  -s dump_scitix.py

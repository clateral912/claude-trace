#!/usr/bin/env bash
# 在"经 mitmproxy 抓包"的环境里启动 Claude Code。
# 只影响这个进程,其它终端/应用完全不受影响。
#
# 用法:  ./run-claude.sh [claude 的参数...]
# 前提:  先在另一个终端跑 ./start-mitm.sh

# 让 claude 的 HTTP/HTTPS 流量走 mitmproxy(8888)
export HTTP_PROXY="http://127.0.0.1:8888"
export HTTPS_PROXY="http://127.0.0.1:8888"
export http_proxy="http://127.0.0.1:8888"
export https_proxy="http://127.0.0.1:8888"

# 关键:必须 unset ALL_PROXY,否则 socks 全局代理(7897)会让 node 绕过 8888
unset ALL_PROXY all_proxy

# 让 claude 这个 Node/Bun 二进制信任 mitmproxy 的 CA
export NODE_EXTRA_CA_CERTS="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"

# ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN 沿用你当前环境(指向 scitix),不改动

exec claude "$@"

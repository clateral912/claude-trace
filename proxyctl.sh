#!/usr/bin/env bash
# 抓包代理 守护控制脚本 —— 默认后台常驻(脱离终端,关窗口也不退)。
#
#   ./proxyctl.sh            # = start
#   ./proxyctl.sh start      # 后台启动看门狗(它再拉起并保活 mitmproxy)
#   ./proxyctl.sh stop       # 停止
#   ./proxyctl.sh restart
#   ./proxyctl.sh status     # 守护 + 端口状态
#   ./proxyctl.sh logs [N]   # 看最近 N 行看门狗日志(默认 40)
#
# 原理:把 watchdog-mitm.sh 以 setsid(无则 nohup)脱离控制终端的方式拉起,记 PID 到 .proxy.pid。
# watchdog 负责保活 mitmproxy;stop 时 kill 看门狗,其 trap 会顺带停掉 mitmproxy。
set -uo pipefail
cd "$(dirname "$0")"

PIDFILE="$(pwd)/.proxy.pid"
LOG="$(pwd)/watchdog.log"
PORT="${MITM_PORT:-8888}"

is_running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; }

status() {
  if is_running; then echo "✓ 守护运行中 (pid $(cat "$PIDFILE"))"
  else echo "✗ 守护未运行"; fi
  if lsof -ti tcp:"$PORT" >/dev/null 2>&1; then echo "✓ 代理端口 $PORT 在监听"
  else echo "✗ 端口 $PORT 无监听"; fi
}

start() {
  if is_running; then echo "已在运行 (pid $(cat "$PIDFILE"))"; status; return 0; fi
  if lsof -ti tcp:"$PORT" >/dev/null 2>&1; then
    echo "⚠ 端口 $PORT 已被占用(可能有残留代理)。先 ./proxyctl.sh stop,或换 MITM_PORT。"; return 1
  fi
  echo "后台启动抓包代理守护…"
  # 用 Python start_new_session=True 让守护进入全新 session(无控制终端),
  # 关掉任何终端都够不到它;其子进程 mitmweb 也在同一新 session,一并受保护。
  # (比 nohup 更彻底:nohup 只是忽略 SIGHUP,不脱离 session;macOS 又没有 setsid。)
  pid=$(python3 -c "
import subprocess
p = subprocess.Popen(
    ['./watchdog-mitm.sh'],
    stdout=open('watchdog.run.log', 'a'), stderr=subprocess.STDOUT,
    stdin=subprocess.DEVNULL, start_new_session=True)
print(p.pid)
")
  echo "$pid" > "$PIDFILE"
  i=0; while [ $i -lt 40 ]; do lsof -ti tcp:"$PORT" >/dev/null 2>&1 && break; sleep 0.5; i=$((i+1)); done
  status
}

stop() {
  local killed=0
  if is_running; then
    echo "停止守护 (pid $(cat "$PIDFILE"))…"
    kill "$(cat "$PIDFILE")" 2>/dev/null && killed=1   # watchdog 的 trap 会顺带停 mitmproxy
    sleep 2
  fi
  for p in $(lsof -ti tcp:"$PORT" 2>/dev/null); do kill "$p" 2>/dev/null; killed=1; done  # 兜底清残留
  rm -f "$PIDFILE"
  [ "$killed" = 1 ] && echo "已停止" || echo "未在运行"
}

case "${1:-start}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; sleep 1; start ;;
  status)  status ;;
  logs)    tail -n "${2:-40}" "$LOG" 2>/dev/null || echo "无日志(还没启动过?)" ;;
  *) echo "用法: $0 {start|stop|restart|status|logs}"; exit 1 ;;
esac

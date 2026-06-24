#!/usr/bin/env bash
# mitmproxy 保活看门狗:监督 start-mitm.sh,每秒检测,异常自动重启。
#
# 用法:
#   ./watchdog-mitm.sh                 # 前台运行(Ctrl-C 停止并清理子进程)
#   nohup ./watchdog-mitm.sh &         # 后台常驻(关终端也不退)
#
# 配置(环境变量):
#   CHECK_INTERVAL   轻量检测间隔(秒):进程存活 + 端口监听   默认 1
#   PROBE_INTERVAL   深度探针间隔(秒):经代理真实 HTTPS 转发  默认 15(设 0 关闭)
#   MITM_PORT        mitmproxy 监听端口                         默认 8888
#   MITM_WEBPORT     mitmweb 网页端口                           默认 8081
#
# 两级健康检查:
#   1) 每 CHECK_INTERVAL 秒:进程还在 + 端口在 listen —— 抓"崩溃/端口关闭"。
#   2) 每 PROBE_INTERVAL 秒:curl 经代理打 scitix,非 000 即正常 ——
#      抓"端口开着但不转发"的半死态(光看端口会漏)。
set -uo pipefail
cd "$(dirname "$0")"

CHECK_INTERVAL="${CHECK_INTERVAL:-1}"
PROBE_INTERVAL="${PROBE_INTERVAL:-15}"
PROBE_FAIL_LIMIT="${PROBE_FAIL_LIMIT:-4}"   # 深度探针需连续失败这么多次才重启(防瞬时抖动误杀)
MITM_PORT="${MITM_PORT:-8888}"
MITM_WEBPORT="${MITM_WEBPORT:-8081}"
export MITM_PORT MITM_WEBPORT
LOG="$(pwd)/watchdog.log"
CA="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"

ts()  { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*" | tee -a "$LOG"; }
notify() { osascript -e "display notification \"$1\" with title \"mitmproxy 看门狗\"" 2>/dev/null || true; }

CHILD=""
start_mitm() {
    ./start-mitm.sh >> mitm.log 2>&1 &
    CHILD=$!
    log "▶ 启动 mitmproxy (pid=$CHILD, port=$MITM_PORT)"
}
stop_mitm() {
    [ -n "$CHILD" ] && kill "$CHILD" 2>/dev/null
    wait "$CHILD" 2>/dev/null
}
cleanup() {
    [ -n "${_CLEANED:-}" ] && return
    _CLEANED=1
    echo
    log "■ 看门狗退出,停止 mitmproxy"
    [ -n "$CHILD" ] && kill "$CHILD" 2>/dev/null
    # 兜底:按 --listen-port 精确杀掉本端口的 mitmproxy,避免残留占端口
    pkill -f "listen-port $MITM_PORT" 2>/dev/null
}
on_signal() { cleanup; exit 0; }
trap on_signal INT TERM
trap cleanup EXIT

# 轻量:进程活 + 端口监听
alive() {
    kill -0 "$CHILD" 2>/dev/null || return 1
    nc -z -w1 127.0.0.1 "$MITM_PORT" 2>/dev/null || return 1
    return 0
}
# 深度:经代理真实访问 scitix(404=转发正常;000=半死/不转发)
forwarding() {
    local code
    code=$(curl -s -m 8 -x "http://127.0.0.1:$MITM_PORT" --cacert "$CA" \
           -H 'X-Mitm-Healthcheck: 1' \
           -o /dev/null -w '%{http_code}' https://api.scitix.ai/model-api 2>/dev/null)
    [ -n "$code" ] && [ "$code" != "000" ]
}

# 启动前确认端口空闲,避免和现有实例双绑
if nc -z -w1 127.0.0.1 "$MITM_PORT" 2>/dev/null; then
    log "⚠️  端口 $MITM_PORT 已被占用。请先停掉现有 mitmproxy(在它的终端按 Ctrl-C)再启动看门狗。"
    exit 1
fi

log "===== 看门狗启动:轻量检测每 ${CHECK_INTERVAL}s,深度探针每 ${PROBE_INTERVAL}s ====="
start_mitm
sleep 3   # 首次启动缓冲,避开启动中间态

secs=0; probe_streak=0
while true; do
    bad=""
    if ! alive; then
        bad="进程退出或端口未监听"; probe_streak=0   # 真崩溃 → 立即重启
    elif [ "$PROBE_INTERVAL" -gt 0 ] && [ $((secs % PROBE_INTERVAL)) -eq 0 ]; then
        # 深度探针:瞬时失败只累计+记日志;连续 PROBE_FAIL_LIMIT 次才判半死重启
        if forwarding; then
            probe_streak=0
        else
            probe_streak=$((probe_streak + 1))
            log "… 转发探针失败 ($probe_streak/$PROBE_FAIL_LIMIT),继续观察(瞬时抖动不重启)"
            [ "$probe_streak" -ge "$PROBE_FAIL_LIMIT" ] && bad="转发探针连续 $probe_streak 次失败(半死态)"
        fi
    fi

    if [ -n "$bad" ]; then
        log "❌ 异常:$bad —— 自动重启中…"
        notify "异常($bad),正在重启"
        stop_mitm
        sleep 1
        start_mitm
        sleep 3
        probe_streak=0
    fi

    sleep "$CHECK_INTERVAL"
    secs=$((secs + CHECK_INTERVAL))
done

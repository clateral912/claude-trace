# Claude Code 抓包包装(fish)
#
# 安装:
#     echo "source /ABS/PATH/TO/myproxy/shell/claude-trace.fish" >> ~/.config/fish/config.fish
# 或临时:
#     source /ABS/PATH/TO/myproxy/shell/claude-trace.fish
#
# 提供两个命令:
#   claude        智能默认:8888 抓包代理在跑就自动被 trace,没跑就警告 + 照常启动
#   claude-trace  强制抓包:代理没跑就直接报错(确保这次一定被 dump)
#
# 前提:先在另一个终端启动 ./start-mitm.sh(监听 127.0.0.1:8888)。
# 可调:CLAUDE_TRACE_PORT(默认 8888)、CLAUDE_TRACE_CA(默认 ~/.mitmproxy/mitmproxy-ca-cert.pem)、
#       CLAUDE_TRACE_ARGS(透传给 claude 的额外固定参数,如 --dangerously-skip-permissions)。

if not set -q CLAUDE_TRACE_PORT;  set -gx CLAUDE_TRACE_PORT 8888;  end
if not set -q CLAUDE_TRACE_CA;    set -gx CLAUDE_TRACE_CA "$HOME/.mitmproxy/mitmproxy-ca-cert.pem";  end

function claude-trace --description "在当前目录启动被 mitmproxy 抓包的 claude"
    if not nc -z 127.0.0.1 $CLAUDE_TRACE_PORT 2>/dev/null
        echo "❌ 抓包代理未运行 (127.0.0.1:$CLAUDE_TRACE_PORT)。请先在另一个终端启动 ./start-mitm.sh" >&2
        return 1
    end
    # 用 env 在子进程里设代理 + CA,不污染当前 shell;-u 移除 socks 兜底,避免绕过抓包端口
    env -u ALL_PROXY -u all_proxy \
        HTTP_PROXY=http://127.0.0.1:$CLAUDE_TRACE_PORT HTTPS_PROXY=http://127.0.0.1:$CLAUDE_TRACE_PORT \
        http_proxy=http://127.0.0.1:$CLAUDE_TRACE_PORT https_proxy=http://127.0.0.1:$CLAUDE_TRACE_PORT \
        NO_PROXY=localhost,127.0.0.1,::1 no_proxy=localhost,127.0.0.1,::1 \
        NODE_EXTRA_CA_CERTS=$CLAUDE_TRACE_CA \
        command claude $CLAUDE_TRACE_ARGS $argv
end

function claude --description "默认走 mitmproxy 抓包;代理未运行则警告并正常启动"
    if nc -z 127.0.0.1 $CLAUDE_TRACE_PORT 2>/dev/null
        claude-trace $argv
    else
        echo "⚠️  抓包代理未运行 (127.0.0.1:$CLAUDE_TRACE_PORT),本次不被 trace(照常启动)。" >&2
        command claude $CLAUDE_TRACE_ARGS $argv
    end
end

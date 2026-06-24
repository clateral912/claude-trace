# claude-trace · Claude Code 流量抓包与可视化

教学/研究用工具集:把 **Claude Code 与某个 LLM 端点之间的 HTTPS 流量**(默认 `api.scitix.ai`)
原样 dump 下来,看清每一轮请求/响应的**裸结构体明文**(system prompt、完整 messages 历史、
工具调用、token 用量、流式 SSE),并能把一次 session 的 **agent 生命周期画成时间轴甘特图**。

底层是 [mitmproxy](https://mitmproxy.org/) 做 TLS 中间人正向代理。**只对目标 host 解密**,
其余流量原样隧道透传——所以你的 pip / curl / 其它工具不受影响、也无需信任任何证书。

> ⚠️ **安全**:`dumps/` 里的请求头含你的真实 `Authorization` / API key,**已被 `.gitignore` 排除,
> 切勿提交或分享**。本仓库只含代码,不含任何抓包数据。

---

## Quickstart

```bash
# ── mitmproxy 需要 Python >= 3.12 ───────────────────────────────────────────
# 最简单其实是用 Homebrew(自带新 Python,可跳过下面一整段):
#     brew install mitmproxy && pip3 install --user matplotlib
# 否则按下面来:自动判断 Python 版本,不够新就用 uv 装一个带 3.12 的本地 venv。

# 1) 检查当前 python3 是否 >= 3.12
if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
    PY=python3                                  # 够新,直接用它
else
    # 2) 不够新:用 uv 创建一个带 Python 3.12 的本地 venv(uv 会自动下载 3.12)
    #    没装 uv 就先装:  curl -LsSf https://astral.sh/uv/install.sh | sh
    uv venv --python 3.12 .venv
    .venv/bin/python -m ensurepip --upgrade     # 确保 venv 里带上 pip
    PY=.venv/bin/python
fi

# 3) 用(够新的)Python 的 pip 安装依赖:mitmproxy + matplotlib
"$PY" -m pip install -r requirements.txt

# 4) 安装 shell 包装(把 claude / claude-trace 写入你的 rc)
./install.sh

# 5) 后台常驻启动抓包代理(进入独立 session,关终端也不停)
./proxyctl.sh start
```

启动后**新开一个终端**,在任意目录直接敲 `claude` 即被抓包(dump 到 `dumps/`);
mitmweb 网页界面在 http://127.0.0.1:8081。`start-mitm.sh` 会自动从 PATH 或本仓库
`.venv/bin` 里找到 `mitmweb`,所以用上面 venv 方式装的也能直接 `./proxyctl.sh start`。

---

## 它能抓到什么

每个请求一个目录 `dumps/<UTC时间戳>_<flowid>/`:

| 文件 | 内容 |
|------|------|
| `meta.json` | method / host / path / 状态码 / 是否流式 / 端到端耗时 `elapsed_ms` |
| `request.headers.json` | 请求头(含 `x-claude-code-session-id`、`user-agent` 等) |
| `request.body.json` | **请求裸结构体**:`system`(系统提示)、`messages`(完整多轮历史)、`tools`、`max_tokens` … |
| `response.headers.json` | 响应头 |
| `response.body.json` | **非流式**响应裸结构体 |
| `response.body.sse` | **流式**响应的原始 SSE 事件流(逐字) |

多轮对话的历史**原样累加在 `messages` 数组**里:用户问题是 `role:"user"`,模型输出(text/thinking/
tool_use)追加为 `role:"assistant"`,工具结果追加为 `role:"user"` 的 `tool_result`。`system` 是
独立的顶层字段(才是真正的 system prompt)。token 用量在响应的 `usage`(`input_tokens` 为
system+messages+tools 总和,另有 `cache_read/creation_input_tokens`)。

---

## 依赖

> ⚠️ **mitmproxy ≥ 10 需要 Python ≥ 3.12**(12.x 要求 `>=3.12`)。macOS 自带 / Xcode 命令行
> 工具里的 Python 通常是 3.9,会报 `Could not find a version that satisfies mitmproxy>=12`
> ——那不是版本写错,是你的 Python 太老。

**推荐(最省事):用 Homebrew,自带新 Python,绕开系统 3.9**
```bash
brew install mitmproxy           # 提供 mitmdump / mitmweb / mitmproxy
pip3 install --user matplotlib   # 仅可视化用;只抓包可不装
```

**或:用 ≥3.12 的 Python 建 venv**
```bash
brew install python@3.12
python3.12 -m venv ~/.venv/claude-trace
~/.venv/claude-trace/bin/pip install -r requirements.txt
```

`start-mitm.sh` 会自动定位 PATH 里的 `mitmweb`。首次运行 mitmproxy 会在 `~/.mitmproxy/` 生成 CA
证书(`mitmproxy-ca-cert.pem`),包装脚本通过 `NODE_EXTRA_CA_CERTS` 让 Claude Code 信任它。

---

## 安装 shell 包装

```bash
./install.sh            # 自动检测当前 shell(bash/zsh/fish)
./install.sh zsh        # 或显式指定
```

会把一行 `source <repo>/shell/claude-trace.{sh,fish}` 写进对应 rc 文件(幂等)。
装好后你得到两个命令:

| 命令 | 行为 |
|------|------|
| `claude` | **智能默认**:抓包代理(8888)在跑就自动被 trace,没跑就打印一行警告、照常启动 |
| `claude-trace` | **强制抓包**:代理没跑就直接报错(确保这次一定被 dump) |

> 这两个包装用 `env` 在**子进程**里设置代理 + CA,**不污染你当前 shell**;并 `unset ALL_PROXY`
> 避免 socks 全局代理绕过抓包端口。手动用 bash/zsh/fish 都不会留下副作用。

---

## 使用

抓包代理**默认后台常驻**(进入独立 session,关掉任何终端都不会停),由 `proxyctl.sh` 管理:

```bash
cd <repo>
./proxyctl.sh start      # 后台启动(含保活看门狗)。网页界面 http://127.0.0.1:8081
./proxyctl.sh status     # 查看守护 + 端口状态
./proxyctl.sh logs       # 看看门狗日志
./proxyctl.sh stop       # 停止
./proxyctl.sh restart
```

启动后,在**任意工作目录**直接用 `claude`,流量就自动 dump 到 `<repo>/dumps/`:

```bash
cd ~/any/project && claude
```

不想被抓时 `./proxyctl.sh stop` 即可,`claude` 会自动降级直连。

> **保活**:`proxyctl.sh start` 拉起的是 `watchdog-mitm.sh`——它监督 mitmproxy,每秒查进程+端口
> 存活、按间隔做真实转发探针,异常自动重启并写 `watchdog.log` + 弹桌面通知。守护用 Python
> `start_new_session` 脱离控制终端,所以关窗口、退出登录 shell 都不影响它(macOS 无 `setsid`,
> 这是可移植的替代)。
>
> 前台调试也行:`./start-mitm.sh`(不常驻,Ctrl-C 即停,会随终端关闭而退出)。

---

## 可视化 session 生命周期

```bash
python3 session_timeline.py            # 自动挑最精彩的窗口(含 subagent 并发)
python3 session_timeline.py --list     # 列候选窗口
python3 session_timeline.py --session <id-prefix> --index 0 --out viz
```

输出 `viz.svg` + `viz.png`:X 轴为时间(idle 间隔压缩),Y 轴为不同 agent——**main 居中,
subagent 上下扇形展开**;每根条 = 一个 turn,条长 = 生成耗时(端到端);条间细线 = client/工具
耗时;▼ 与虚线箭头标记 subagent 派发(fan-out)。

---

## 配置项(环境变量)

| 变量 | 默认 | 作用 | 用在哪 |
|------|------|------|--------|
| `ALLOW_HOSTS` | `scitix` | **只对匹配该正则的 host 做 MITM**,其余透传。抓 Anthropic 直连改 `'scitix\|anthropic'` | `start-mitm.sh` |
| `UPSTREAM_PROXY` | `http://127.0.0.1:7897` | 上游代理(链到你已有的科学上网代理);没有就设为空走直连 | `start-mitm.sh` |
| `MITM_PORT` / `MITM_WEBPORT` | `8888` / `8081` | 代理端口 / 网页端口 | `start-mitm.sh` |
| `DUMP_DIR` | `<repo>/dumps` | 落盘目录 | `start-mitm.sh` |
| `DUMP_TARGET_HOST` | `scitix` | addon 只 dump host 含此子串的流量 | `dump_scitix.py` |
| `CLAUDE_TRACE_PORT` | `8888` | 包装脚本探测/路由的抓包端口 | `shell/*` |
| `CLAUDE_TRACE_CA` | `~/.mitmproxy/mitmproxy-ca-cert.pem` | 让 claude 信任的 CA | `shell/*` |
| `CLAUDE_TRACE_ARGS` | (空) | 透传给 claude 的固定参数(如 `--dangerously-skip-permissions`) | `shell/*` |
| `CHECK_INTERVAL` / `PROBE_INTERVAL` / `PROBE_FAIL_LIMIT` | `1` / `15` / `4` | 看门狗检测节奏 | `watchdog-mitm.sh` |

---

## 文件一览

```
proxyctl.sh           守护控制(start/stop/restart/status/logs)—— 默认后台常驻入口
start-mitm.sh         启动 mitmproxy(只 MITM 目标 host,其余透传;不自动开浏览器)
dump_scitix.py        mitmproxy addon:只 dump 目标 host 流量,流式用 tee 不破坏实时性
watchdog-mitm.sh      保活看门狗(自动重启 + 日志 + 通知)
session_timeline.py   把一次 session 画成 agent 时间轴甘特图(SVG+PNG)
install.sh            把 shell 包装装进 rc 文件
shell/claude-trace.sh    bash / zsh 包装
shell/claude-trace.fish  fish 包装
requirements.txt      mitmproxy, matplotlib
archive/              早期方案:Anthropic 兼容反向代理(改 ANTHROPIC_BASE_URL),留作参考
```

---

## 抓不到 / 排错

- **HTTPS_PROXY 指向 8888 但代理没跑** → claude 卡住或报错。用 `claude`(智能包装)而非裸 `claude`,
  或确认 `start-mitm.sh` 在跑。
- **非目标站点 TLS 报错(60)** → 说明它被 MITM 了;确认 `ALLOW_HOSTS` 只含目标 host(默认只 `scitix`)。
- **抓不到 Claude 直连 Anthropic 的流量** → 默认只解密 scitix;设 `ALLOW_HOSTS='scitix|anthropic'` 重启。
- **dump 没增长** → 确认 claude 经 8888(用 `claude-trace` 会在没代理时直接报错,便于发现)。

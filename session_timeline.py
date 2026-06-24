#!/usr/bin/env python3
"""Visualize the lifespan of one Claude Code session as an agent-swimlane timeline.

X = time, with global-idle gaps COMPRESSED (intervals where no agent is generating
are squashed to a small cap, so the picture stays readable). Y = agents: the main
agent sits on the center axis and subagents fan out above/below it. Each turn is a
fixed-height bar whose width = generation time (router-receives-request ->
SGLang-finishes-returning), taken from the proxy-measured end-to-end elapsed_ms.
Thin connectors between a lane's bars are client-side time (tool exec / sandbox /
env) and are intentionally unlabeled. ▼ + dotted arrows mark subagent dispatch.

NOTE: this dataset (mitmproxy dumps) has no engine-internal OTel spans, so bars are
NOT split into prefill/decode — each bar is a single generation block.

Usage:
  python3 session_timeline.py                 # auto-pick the best window
  python3 session_timeline.py --list          # rank candidate windows
  python3 session_timeline.py --session c19a326c --index 0 --out viz

Inputs:  myproxy/dumps/<ts>_<id>/{meta,request.headers,request.body,response.body}.*
Outputs: <out>.svg and <out>.png
"""
import argparse
import glob
import hashlib
import json
import os
from collections import defaultdict
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
DUMPS_DIR_DEFAULT = os.path.join(ROOT, "dumps")
BIG_CLEN = 20000          # Content-Length threshold for "real" turn (concurrency stat)
GAP_DEFAULT = 600         # idle-gap segmentation threshold (seconds)
DISPATCH_TOOLS = {"Task", "Agent", "Workflow"}   # tools that fan out subagents

C_CONN = "#64748b"        # connector (client time)
C_DISPATCH = "#dc2626"    # dispatch marker / arrow
LANE_COLORS = {"main": "#0f172a", "sub": "#7c3aed", "util": "#9ca3af"}


# ============================================================ 1. load + index
def load_index(dumps_dir):
    """Cheap meta+headers pass over all dumps -> time-sorted turn stubs."""
    turns = []
    for d in sorted(os.listdir(dumps_dir)):
        base = os.path.join(dumps_dir, d)
        mp = os.path.join(base, "meta.json")
        if not os.path.isfile(mp):
            continue
        try:
            m = json.load(open(mp))
        except Exception:
            continue
        if not m.get("time_utc") or not m.get("elapsed_ms"):
            continue
        hl = {}
        try:
            h = json.load(open(os.path.join(base, "request.headers.json")))
            hl = {k.lower(): v for k, v in h.items()}
        except Exception:
            pass
        try:
            clen = int(hl.get("content-length", "0") or 0)
        except ValueError:
            clen = 0
        st = datetime.fromisoformat(m["time_utc"])
        gen = m["elapsed_ms"] / 1000.0
        turns.append(dict(
            dir=d, base=base,
            sid=hl.get("x-claude-code-session-id") or "none",
            t_req=st.timestamp(), t_resp=st.timestamp() + gen, gen=gen,
            clen=clen, status=m.get("status_code")))
    turns.sort(key=lambda t: t["t_req"])
    return turns


def segment_by_gap(turns, gap_s):
    if not turns:
        return []
    segs, cur = [], [turns[0]]
    for prev, t in zip(turns, turns[1:]):
        if t["t_req"] - prev["t_resp"] > gap_s:
            segs.append(cur)
            cur = [t]
        else:
            cur.append(t)
    segs.append(cur)
    return segs


def max_concurrency(turns):
    ev = []
    for t in turns:
        if t["clen"] >= BIG_CLEN:
            ev += [(t["t_req"], 1), (t["t_resp"], -1)]
    ev.sort(key=lambda x: (x[0], -x[1]))
    c = mx = 0
    for _, dx in ev:
        c += dx
        mx = max(mx, c)
    return mx


def list_candidates(all_turns, gap_s):
    by_sid = defaultdict(list)
    for t in all_turns:
        by_sid[t["sid"]].append(t)
    cands = []
    for sid, ts in by_sid.items():
        for seg in segment_by_gap(sorted(ts, key=lambda x: x["t_req"]), gap_s):
            cands.append(dict(seg=seg, sid=sid, conc=max_concurrency(seg), n=len(seg),
                              mins=(seg[-1]["t_resp"] - seg[0]["t_req"]) / 60))
    cands.sort(key=lambda c: (c["conc"] >= 3 and 15 <= c["n"] <= 220, c["conc"],
                              -abs(c["n"] - 120)), reverse=True)
    return cands


# ============================================================ 2. enrich (read body/resp)
def _stringify(c):
    return c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)


def parse_response(base):
    """From response.body.sse / .json: output_tokens, stop_reason, tool names."""
    out = {"out_tok": None, "finish": None, "tools": [], "text": ""}
    sse = os.path.join(base, "response.body.sse")
    js = os.path.join(base, "response.body.json")
    if os.path.isfile(sse):
        for line in open(sse, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except Exception:
                continue
            tp = ev.get("type")
            if tp == "content_block_start":
                cb = ev.get("content_block", {})
                if cb.get("type") == "tool_use":
                    out["tools"].append(cb.get("name"))
            elif tp == "content_block_delta":
                d = ev.get("delta", {})
                if d.get("type") == "text_delta":
                    out["text"] += d.get("text", "")
            elif tp == "message_delta":
                d = ev.get("delta", {})
                if d.get("stop_reason"):
                    out["finish"] = d["stop_reason"]
                ot = ev.get("usage", {}).get("output_tokens")
                if ot is not None:
                    out["out_tok"] = ot
    elif os.path.isfile(js):
        try:
            j = json.load(open(js))
            out["out_tok"] = (j.get("usage") or {}).get("output_tokens")
            out["finish"] = j.get("stop_reason")
            for blk in j.get("content", []):
                if blk.get("type") == "tool_use":
                    out["tools"].append(blk.get("name"))
                elif blk.get("type") == "text":
                    out["text"] += blk.get("text", "")
        except Exception:
            pass
    out["text"] = out["text"].strip()[:160]
    return out


def enrich(turns):
    for t in turns:
        try:
            b = json.load(open(os.path.join(t["base"], "request.body.json")))
        except Exception:
            b = {}
        msgs = b.get("messages", [])
        sys_full = _stringify(b.get("system", ""))
        m0_full = _stringify(msgs[0].get("content", "")) if msgs else ""
        r = parse_response(t["base"])
        # 线程指纹只用 messages[0](会话首条,永不随轮次变化);system 含每请求
        # 易变内容(类似 billing/cch 前缀),放进 key 会把每个 turn 拆成独立线程。
        t.update(
            fp=hashlib.md5(m0_full.encode()).hexdigest()[:10],
            sys_full=sys_full, m0=m0_full, nmsg=len(msgs),
            model=b.get("model", "?"),
            tools=r["tools"], out_tok=r["out_tok"], finish=r["finish"], text=r["text"])
    return turns


# ============================================================ 3. lane assignment
def assign_lanes(turns):
    """Thread turns into agent lanes by (system, first message); pick main=深度最大
    的会话(nmsg 远大于 subagent);把后台小调用归为 utility 合并到一条。"""
    enrich(turns)
    threads = defaultdict(list)
    for t in turns:
        threads[t["fp"]].append(t)

    lanes = []
    for fp, ts in threads.items():
        ts.sort(key=lambda x: x["t_req"])
        nonweb = [tc for t in ts for tc in t["tools"] if tc not in ("web_search", "web_fetch")]
        web_only = (len(ts) <= 2 and not nonweb
                    and any(tc in ("web_search", "web_fetch") for t in ts for tc in t["tools"]))
        is_util = web_only or (len(ts) <= 2 and all(not t["tools"] for t in ts)
                               and all(t["nmsg"] <= 4 for t in ts))
        lanes.append(dict(fp=fp, turns=ts, is_util=is_util,
                          start=ts[0]["t_req"], maxmsg=max(t["nmsg"] for t in ts)))

    work = [l for l in lanes if not l["is_util"]]
    out = []
    if work:
        main = max(work, key=lambda l: l["maxmsg"])   # 主线=对话深度最大者(几百条 vs 子的几条)
        main["kind"], main["label"] = "main", "main agent"
        out.append(main)
        for i, s in enumerate(sorted([l for l in work if l is not main],
                                     key=lambda l: l["start"]), 1):
            s["kind"], s["label"] = "sub", f"subagent {i}"
            out.append(s)
    util = [l for l in lanes if l["is_util"]]
    if util:
        out.append(dict(kind="util", label="utility (title/quota)",
                        turns=sorted([t for u in util for t in u["turns"]],
                                     key=lambda x: x["t_req"])))
    return out


# ============================================================ 4. time-warp (compress idle)
def build_warp(turns, t0, idle_cap=2.0):
    """Piecewise-linear real->display map: busy intervals (≥1 lane generating) at
    scale 1; global-idle gaps capped to idle_cap display seconds."""
    busy = sorted([(t["t_req"], t["t_resp"]) for t in turns])
    merged = []
    for a, b in busy:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    pts_r, pts_d = [t0], [0.0]
    cur_r, cur_d = t0, 0.0
    for a, b in merged:
        if a > cur_r:
            cur_d += min(a - cur_r, idle_cap)
            cur_r = a
            pts_r.append(cur_r)
            pts_d.append(cur_d)
        cur_d += (b - cur_r)
        cur_r = b
        pts_r.append(cur_r)
        pts_d.append(cur_d)

    def warp(t):
        if t <= pts_r[0]:
            return pts_d[0]
        for i in range(1, len(pts_r)):
            if t <= pts_r[i]:
                r0, r1, d0, d1 = pts_r[i - 1], pts_r[i], pts_d[i - 1], pts_d[i]
                return d1 if r1 == r0 else d0 + (d1 - d0) * (t - r0) / (r1 - r0)
        return pts_d[-1]

    def unwarp(d):
        if d <= pts_d[0]:
            return pts_r[0]
        for i in range(1, len(pts_d)):
            if d <= pts_d[i]:
                d0, d1, r0, r1 = pts_d[i - 1], pts_d[i], pts_r[i - 1], pts_r[i]
                return r1 if d1 == d0 else r0 + (r1 - r0) * (d - d0) / (d1 - d0)
        return pts_r[-1]

    return warp, unwarp


# ============================================================ 5. plot
def plot(sid, lanes, out_base, title_extra=""):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    from matplotlib.lines import Line2D
    from matplotlib import font_manager

    for fp in ["/System/Library/Fonts/PingFang.ttc",
               "/Library/Fonts/Arial Unicode.ttf",
               "/System/Library/Fonts/Hiragino Sans GB.ttc"]:
        if os.path.exists(fp):
            try:
                font_manager.fontManager.addfont(fp)
                plt.rcParams["font.family"] = font_manager.FontProperties(fname=fp).get_name()
                break
            except Exception:
                pass
    plt.rcParams["axes.unicode_minus"] = False

    all_turns = [t for l in lanes for t in l["turns"]]
    t0 = min(t["t_req"] for t in all_turns)
    warp, unwarp = build_warp(all_turns, t0)
    disp_max = warp(max(t["t_resp"] for t in all_turns))

    BAR_H = 0.52
    fig_w = max(14, min(34, disp_max * 0.13 + 4))
    fig_h = 1.8 + 1.05 * len(lanes)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))

    # fan-out 布局: main 居中(y=0), subagent 上下交替, utility 沉到最下
    main = next((l for l in lanes if l["kind"] == "main"), None)
    subs = [l for l in lanes if l["kind"] == "sub"]
    utils = [l for l in lanes if l["kind"] == "util"]
    if main:
        main["y"] = 0.0
    for i, s in enumerate(subs):
        k = i // 2 + 1
        s["y"] = float(k) if i % 2 == 0 else float(-k)
    occ = [l["y"] for l in lanes if "y" in l]
    ymin = min(occ) if occ else 0.0
    for j, u in enumerate(utils):
        u["y"] = ymin - 1.0 - j

    ylabels = []
    for lane in lanes:
        y = lane["y"]
        col = LANE_COLORS[lane["kind"]]
        ylabels.append((y, lane["label"]))
        prev_end = None
        for ti, t in enumerate(sorted(lane["turns"], key=lambda x: x["t_req"]), 1):
            x0, x1 = warp(t["t_req"]), warp(t["t_resp"])
            w = max(x1 - x0, 0.04)
            if prev_end is not None:                       # connector = client time
                ax.plot([prev_end, x0], [y, y], color=C_CONN, lw=1.2, zorder=1,
                        solid_capstyle="round")
            prev_end = x1
            ax.add_patch(Rectangle((x0, y - BAR_H / 2), w, BAR_H, facecolor=col,
                                   edgecolor="white", lw=0.4, zorder=2))
            # turn 起点小刻度,每 5 个标号
            ax.plot([x0, x0], [y + BAR_H / 2, y + BAR_H / 2 + 0.13], color="#334155",
                    lw=0.8, zorder=6)
            if ti % 5 == 0:
                ax.text(x0, y + BAR_H / 2 + 0.17, str(ti), ha="center", va="bottom",
                        fontsize=6, color="#334155", zorder=6)
            if DISPATCH_TOOLS & set(t["tools"]):           # 派发标记
                ax.plot(x1, y, marker="v", color=C_DISPATCH, ms=8, zorder=5)

    # 派发箭头: 每个 subagent 连到「它启动前最近一次派发」的 lane(支持嵌套派发)
    dispatchers = [(t["t_resp"], l["y"]) for l in lanes if l["kind"] in ("main", "sub")
                   for t in l["turns"] if DISPATCH_TOOLS & set(t["tools"])]
    for s in subs:
        s_start = min(t["t_req"] for t in s["turns"])
        cand = [(tr, yy) for tr, yy in dispatchers if tr <= s_start + 1]
        tr, y_from = max(cand, key=lambda z: z[0]) if cand else (s_start, main["y"] if main else 0.0)
        ax.annotate("", xy=(warp(s_start), s["y"]), xytext=(warp(tr), y_from),
                    arrowprops=dict(arrowstyle="->", color=C_DISPATCH, lw=1.0,
                                    ls=":", alpha=0.8), zorder=4)

    for y, _ in ylabels:
        ax.axhline(y, color="#f1f5f9", lw=22, zorder=0)
    ys = [y for y, _ in ylabels]
    ax.set_yticks(ys)
    ax.set_yticklabels([lab for _, lab in ylabels], fontsize=10)
    ax.set_ylim(min(ys) - 0.7, max(ys) + 0.8)

    # x ticks: 锚在 turn 起点, 按显示坐标最小间距贪心抽稀(不重叠也不跳秒)
    cands = sorted({warp(t["t_req"]) for t in all_turns} | {0.0, disp_max})
    min_gap = disp_max / 14.0
    xticks, last = [], -1e9
    for d in cands:
        if d - last >= min_gap:
            xticks.append(d)
            last = d
    ax.set_xticks(xticks)
    ax.set_xticklabels([f"{unwarp(d) - t0:.0f}s" for d in xticks], fontsize=8)
    ax.set_xlim(-0.5, disp_max + 0.5)
    ax.set_xlabel("time since window start (s) · idle gaps compressed; busy time at full scale",
                  fontsize=9)

    n_turns = sum(len(l["turns"]) for l in lanes if l["kind"] != "util")
    ax.set_title(f"Session lifespan · {sid[:12]}{title_extra} · {n_turns} turns · "
                 f"main + {len(subs)} subagent(s)\n"
                 f"bar width = generation (router→SGLang, end-to-end) · "
                 f"connector = client/tool time · ▼ = subagent dispatch",
                 fontsize=11, loc="left")

    legend = [
        Line2D([0], [0], color=LANE_COLORS["main"], lw=8, label="main agent turn"),
        Line2D([0], [0], color=LANE_COLORS["sub"], lw=8, label="subagent turn"),
        Line2D([0], [0], color=LANE_COLORS["util"], lw=8, label="utility (title/quota)"),
        Line2D([0], [0], color=C_CONN, lw=2, label="client / tool exec time"),
        Line2D([0], [0], marker="v", color="w", markerfacecolor=C_DISPATCH, ms=9,
               label="subagent dispatch"),
    ]
    ax.legend(handles=legend, loc="upper center", bbox_to_anchor=(0.5, -0.16),
              ncol=5, fontsize=8, frameon=False)
    for sp in ("top", "right", "left"):
        ax.spines[sp].set_visible(False)
    ax.tick_params(axis="y", length=0)
    plt.tight_layout()
    fig.savefig(out_base + ".svg", bbox_inches="tight")
    fig.savefig(out_base + ".png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("wrote", out_base + ".svg", "and .png")


# ============================================================ CLI
def main():
    ap = argparse.ArgumentParser(description="Session lifespan timeline (Claude Code dumps)")
    ap.add_argument("--dumps", default=DUMPS_DIR_DEFAULT)
    ap.add_argument("--session", help="session-id prefix filter")
    ap.add_argument("--gap", type=int, default=GAP_DEFAULT)
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--out", default="session_timeline")
    args = ap.parse_args()

    all_turns = load_index(args.dumps)
    if args.session:
        all_turns = [t for t in all_turns if t["sid"].startswith(args.session)]
    cands = list_candidates(all_turns, args.gap)
    if not cands:
        print("no candidate windows")
        return

    print(f"candidates (top 10):  {'#':>2} {'session':<14}{'turn':>5}{'min':>6}{'conc':>5}  start")
    for i, c in enumerate(cands[:10]):
        print(f"  {i:>2} {c['sid'][:12]:<14}{c['n']:>5}{c['mins']:>6.1f}{c['conc']:>5}  "
              f"{datetime.fromtimestamp(c['seg'][0]['t_req']).strftime('%m-%d %H:%M')}")
    if args.list:
        return

    c = cands[args.index]
    lanes = assign_lanes(c["seg"])
    when = datetime.fromtimestamp(c["seg"][0]["t_req"]).strftime("%m-%d %H:%M")
    print(f"\nrendering #{args.index}: {c['sid'][:12]} {when} · {c['n']} turns · conc {c['conc']}")
    for l in lanes:
        print(f"  [{l['kind']:4s}] {l['label']:22s} n={len(l['turns']):2d} "
              f"gen_sum={sum(t['gen'] for t in l['turns']):.1f}s")
    if args.debug:
        return
    plot(c["sid"], lanes, args.out, title_extra=f" · {when}")


if __name__ == "__main__":
    main()

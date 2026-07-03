#!/usr/bin/env python3
"""荔枝争运战回放清洗 + 自动诊断工具。

用法:
    python3 tools/replay_analyzer.py "log/replay (27).7z"      # 单局完整诊断
    python3 tools/replay_analyzer.py log/ --latest             # 目录里最新一局
    python3 tools/replay_analyzer.py log/ --all                # 全部对局汇总表
    python3 tools/replay_analyzer.py <replay> --json           # 机器可读输出
    可选 --me <playerId>（默认自动识别 team- 前缀队伍）

功能:
    清洗: 7z(含分卷)/txt 自动解包 → 标准化时间线（状态段/事件流）
    诊断: 把历次败局复盘中人工使用的取证模式固化为规则引擎——
          比分归因 / 中边冻结 / 罚站 / 冰鉴战争 / 任务节奏 / 回头 /
          窗口战 / 强通耗时 / 设卡攻防 / 交付风险，每条发现附严重度
          与对应策略文档章节（client/docs/strategy.html）。

依赖: 标准库；解 .7z 需要 py7zr（pip install py7zr，仅开发机用）。
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict

# ---------------------------------------------------------------- 数据装载

def _extract_7z(path, workdir):
    try:
        import py7zr
    except ImportError:
        sys.exit("解 .7z 需要 py7zr: pip install py7zr")
    # 分卷: foo.7z.001/.002 → 拼接
    if re.search(r"\.7z\.\d+$", path):
        base = re.sub(r"\.\d+$", "", path)
        parts = sorted(p for p in os.listdir(os.path.dirname(path) or ".")
                       if p.startswith(os.path.basename(base)))
        combined = os.path.join(workdir, "combined.7z")
        with open(combined, "wb") as out:
            for part in parts:
                with open(os.path.join(os.path.dirname(path) or ".", part), "rb") as f:
                    out.write(f.read())
        path = combined
    with py7zr.SevenZipFile(path) as z:
        names = [n for n in z.getnames() if n.endswith(".txt")]
        z.extractall(workdir)
    return [os.path.join(workdir, n) for n in names]


def load_replay(path):
    """返回 (静态首行 dict, [逐帧 dict], 终局 dict)。"""
    if path.endswith(".7z") or re.search(r"\.7z\.\d+$", path):
        import tempfile
        workdir = tempfile.mkdtemp(prefix="replay_")
        txts = _extract_7z(path, workdir)
        if not txts:
            sys.exit(f"{path}: 压缩包里没有 .txt")
        path = txts[0]
    lines = [l.strip() for l in open(path, encoding="utf-8") if l.strip()]
    first, last = json.loads(lines[0]), json.loads(lines[-1])
    rounds = [json.loads(l) for l in lines[1:-1]]
    return first, rounds, last


def find_replays(directory):
    out = []
    for fn in os.listdir(directory):
        if re.match(r"replay.*\.(7z|txt)$", fn) and not re.search(r"\.7z\.\d+$", fn):
            out.append(os.path.join(directory, fn))
    # 同名 txt/7z 去重，txt 优先（免解包）
    seen, uniq = set(), []
    for p in sorted(out, key=lambda x: (re.sub(r"\.(7z|txt)$", "", x), x.endswith(".7z"))):
        key = re.sub(r"\.(7z|txt)$", "", p)
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    def num(p):
        m = re.search(r"\((\d+)\)", p)
        return int(m.group(1)) if m else -1
    return sorted(uniq, key=num)

# ---------------------------------------------------------------- 清洗

class Match:
    def __init__(self, first, rounds, last, me_id=None):
        self.first, self.rounds, self.last = first, rounds, last
        self.match_id = first.get("matchId", "?")
        players = last.get("players") or first.get("players") or []
        self.me_id = me_id or self._guess_me(players)
        self.opp_id = next((p.get("playerId") or p.get("id") for p in players
                            if (p.get("playerId") or p.get("id")) != self.me_id), None)
        self.me_final = self._final(self.me_id)
        self.opp_final = self._final(self.opp_id)
        self.timeline = self._timeline(self.me_id)
        self.opp_timeline = self._timeline(self.opp_id)
        self.events = self._events()

    @staticmethod
    def _guess_me(players):
        for p in players:
            name = (p.get("playerName") or p.get("name") or "")
            if name.startswith("team-"):
                return p.get("playerId") or p.get("id")
        return players[0].get("playerId") if players else None

    def _final(self, pid):
        for p in self.last.get("players") or []:
            if (p.get("playerId") or p.get("id")) == pid:
                return p
        return {}

    def _timeline(self, pid):
        """状态段: [(起帧, 止帧, node, next, state, progressMs)]（相邻同状态合并）"""
        segs = []
        for d in self.rounds:
            rnd = d.get("round")
            for p in d.get("players") or []:
                if (p.get("playerId") or p.get("id")) != pid:
                    continue
                key = (p.get("currentNodeId"), p.get("nextNodeId"), p.get("state"))
                prog = p.get("edgeProgressMs")
                if segs and tuple(segs[-1][2:5]) == key:
                    segs[-1][1] = rnd
                    segs[-1][5].append(prog)
                else:
                    segs.append([rnd, rnd, *key, [prog]])
        return segs

    def _events(self):
        out = []
        for d in self.rounds:
            rnd = d.get("round")
            for e in d.get("messages") or []:
                out.append((rnd, e.get("type", ""), e.get("payload") or {}))
        return out

    def my_events(self, *types):
        return [(r, t, p) for r, t, p in self.events
                if (not types or t in types) and p.get("playerId") == self.me_id]

    def opp_events(self, *types):
        return [(r, t, p) for r, t, p in self.events
                if (not types or t in types) and p.get("playerId") == self.opp_id]

# ---------------------------------------------------------------- 诊断规则

SEV = {"CRIT": "🔴", "WARN": "🟡", "INFO": "🔵", "GOOD": "🟢"}


def diagnose(m: Match):
    """返回 findings: [(严重度, 标题, 详情, 文档章节)]。"""
    f = []
    me, opp = m.me_final, m.opp_final
    my_sd, op_sd = me.get("scoreDetail") or {}, opp.get("scoreDetail") or {}

    # ---- 0. 胜负与比分归因 ----
    my_total, op_total = me.get("totalScore", 0), opp.get("totalScore", 0)
    won = my_total > op_total
    diffs = sorted(((k, my_sd.get(k, 0) - op_sd.get(k, 0))
                    for k in ("delivery", "freshness", "goodFruit", "time",
                              "tasks", "bounty", "penalty")),
                   key=lambda x: x[1])
    attribution = "  ".join(f"{k}{'%+d' % v}" for k, v in diffs if v)
    f.append(("GOOD" if won else "CRIT",
              f"{'胜' if won else '负'} {my_total}:{op_total}",
              f"分项差: {attribution or '全平'}", "§3.1"))
    if not me.get("delivered"):
        f.append(("CRIT", "未交付!", "送达/好果/鲜度/用时全部归零", "§3.2"))
    if (me.get("penaltyScore") or 0) > 0:
        f.append(("WARN", f"惩罚 -{me['penaltyScore']}", "检查非法动作/交付后违规", "§9"))

    # ---- 1. 中边冻结（历史最致命：replay20 冻到终场）----
    # 段内游程扫描：MOVING 且 edgeProgressMs 连续 ≥15 帧不变
    for seg in m.timeline:
        r0, r1, node, nxt, state, progs = seg
        if state != "MOVING" or r1 - r0 < 15:
            continue
        run_start, run_val = r0, progs[0]
        for i, v in enumerate(progs + [None]):
            rnd = r0 + i
            if v != run_val:
                if run_val is not None and rnd - run_start >= 15:
                    guard_info = _guard_at(m, nxt, run_start, rnd)
                    f.append(("CRIT",
                              f"中边冻结 {run_start}~{rnd} ({rnd-run_start}帧) "
                              f"于 {node}→{nxt} (进度 {run_val})",
                              f"目标节点卡: {guard_info or '未见卡(数据异常?)'}；"
                              f"应触发削弱解冻或防陷阱等待", "§5/§9防御表"))
                run_start, run_val = rnd, v

    # ---- 2. 罚站/蹲刷（区分正当与误伤） ----
    gate = _gate_node(m)
    for seg in m.timeline:
        r0, r1, node, nxt, state, _ = seg
        dur = r1 - r0
        if state in ("WAITING", "IDLE") and dur >= 12 and node not in (gate, "S15"):
            opp_pos = _opp_node_at(m, r0)
            label = "对手相关等待" if opp_pos else "原地等待"
            f.append(("WARN", f"{label} {r0}~{r1} ({dur}帧) @{node}",
                      f"当时对手在 {opp_pos or '?'}；若对手无设卡前科则疑似防陷阱误伤，"
                      f"若在任务候选点且我方落后则可能是蹲刷", "§9防御表/V3.9-3.11"))

    # ---- 3. 冰鉴战争 ----
    my_ice = [(r, p.get("nodeId")) for r, t, p in m.my_events("RESOURCE_CLAIM")
              if p.get("resourceType") == "ICE_BOX"]
    op_ice = [(r, p.get("nodeId")) for r, t, p in m.opp_events("RESOURCE_CLAIM")
              if p.get("resourceType") == "ICE_BOX"]
    fresh_d = round((me.get("freshness") or 0) - (opp.get("freshness") or 0), 1)
    sev = "GOOD" if len(my_ice) >= len(op_ice) else \
          ("CRIT" if len(op_ice) - len(my_ice) >= 2 else "WARN")
    f.append((sev, f"冰鉴 {len(my_ice)}:{len(op_ice)} 鲜度差 {fresh_d:+}",
              f"我方 {my_ice or '无'} | 对方 {op_ice or '无'}", "§3.3/V3.2-3.3"))

    # ---- 4. 任务节奏（尾段任务荒检测） ----
    my_tasks = [r for r, t, p in m.my_events("TASK_COMPLETE")]
    op_tasks = [r for r, t, p in m.opp_events("TASK_COMPLETE")]
    base_d = (me.get("taskScore") or 0) - (opp.get("taskScore") or 0)
    sev = "GOOD" if base_d >= 0 else ("CRIT" if base_d <= -60 else "WARN")
    f.append((sev, f"任务基础分 {me.get('taskScore')}:{opp.get('taskScore')} "
                   f"(完成 {len(my_tasks)}:{len(op_tasks)})",
              f"我方完成帧 {my_tasks} | 对方 {op_tasks}", "§3.1"))
    if my_tasks:
        last_t = max(my_tasks)
        deliver_r = me.get("deliverRound") or 600
        drought = deliver_r - last_t
        op_in_window = [r for r in op_tasks if r > last_t]
        if drought > 150 and len(op_in_window) >= 2:
            f.append(("CRIT", f"尾段任务荒: r{last_t} 后 {drought} 帧零任务",
                      f"同期对手完成 {len(op_in_window)} 个 ({op_in_window})；"
                      f"任务刷新跟在车队身后——考虑跟随者蹲刷是否生效", "V3.10-3.11"))

    # ---- 5. 回头检测 ----
    visits = []
    for seg in m.timeline:
        if not seg[3] and seg[2]:  # 停靠段
            if not visits or visits[-1][0] != seg[2]:
                visits.append((seg[2], seg[0]))
    seen = {}
    for node, rnd in visits:
        if node in seen and rnd - seen[node] < 120:
            f.append(("WARN", f"回头: r{seen[node]} 离开 {node} 后 r{rnd} 折返",
                      "检查回头迟滞是否生效/估值是否近似打平", "V3.8"))
        seen[node] = rnd

    # ---- 6. 窗口战 ----
    wins = losses = draws = 0
    for r, t, p in m.events:
        if t == "WINDOW_CONTEST_END":
            w = p.get("winnerTeamId")
            my_team = _my_team(m)
            if w == "DRAW":
                draws += 1
            elif w == my_team:
                wins += 1
            else:
                losses += 1
    if wins + losses + draws:
        sev = "GOOD" if wins >= losses else "WARN"
        f.append((sev, f"窗口战 {wins}胜{losses}负{draws}平",
                  "S02 起跑窗口决定走廊归属，重点复盘首个窗口", "§8"))

    # ---- 7. 强通/攻坚/设卡 ----
    for r, t, p in m.my_events("FORCED_PASS_END"):
        start = next((r2 for r2, t2, p2 in m.my_events("FORCED_PASS_START")
                      if r2 < r), None)
        dur = (r - start) if start else "?"
        sev = "CRIT" if isinstance(dur, int) and dur > 60 else "WARN"
        f.append((sev, f"强制通行 {start}~{r} ({dur}帧) → {p.get('nodeId')}",
                  "对比削弱路径(约32帧+边程)是否更快", "§5/V3.8"))
    my_guards = [(r, p.get("nodeId")) for r, t, p in m.my_events("GUARD_SET")]
    op_guards = [(r, p.get("nodeId")) for r, t, p in m.opp_events("GUARD_SET")]
    my_breaks = [(r, p.get("nodeId")) for r, t, p in m.my_events("GUARD_BREAK")]
    if my_guards or op_guards or my_breaks:
        f.append(("INFO", f"设卡战 我设{my_guards or '无'} 敌设{op_guards or '无'} "
                          f"我破{my_breaks or '无'}",
                  "我方设卡应出现在领先通过咽喉后", "§6"))
    if op_guards and not my_guards:
        f.append(("WARN", "对手设卡而我们全场未设",
                  "领先时段是否存在却未触发设卡机会？", "§6/V3.7"))

    # ---- 8. 交付时间 ----
    my_d, op_d = me.get("deliverRound") or 0, opp.get("deliverRound") or 0
    if my_d and op_d:
        f.append(("INFO" if my_d <= op_d else "WARN",
                  f"交付 {my_d} vs {op_d} ({my_d - op_d:+}帧)",
                  "时间分斜率仅 0.117/帧，任务>速度", "§3.1"))
    elif my_d and my_d > 570:
        f.append(("WARN", f"交付过晚 r{my_d}", "复盘拖延源(冻结/罚站/绕路)", ""))
    return f


def _gate_node(m):
    gp = ((m.first.get("map") or {}).get("gameplay") or {})
    return (gp.get("roles") or {}).get("gateNodeId", "S14")


def _my_team(m):
    for p in m.last.get("players") or []:
        if (p.get("playerId") or p.get("id")) == m.me_id:
            return "RED" if p.get("camp") == 0 else "BLUE"
    return None


def _opp_node_at(m, rnd):
    for seg in m.opp_timeline:
        if seg[0] <= rnd <= seg[1]:
            return seg[2] + (f"→{seg[3]}" if seg[3] else "")
    return None


def _guard_at(m, node, r0, r1):
    for d in m.rounds:
        rnd = d.get("round")
        if rnd and r0 <= rnd <= r1:
            for n in d.get("nodes") or []:
                if n.get("nodeId") == node and n.get("guard"):
                    g = n["guard"]
                    return f"{g.get('ownerTeamId')} 防{g.get('defense')}"
    return None

# ---------------------------------------------------------------- 输出

def report(m: Match, as_json=False):
    findings = diagnose(m)
    if as_json:
        print(json.dumps({
            "matchId": m.match_id,
            "me": m.me_id, "opp": m.opp_id,
            "myScore": m.me_final.get("totalScore"),
            "oppScore": m.opp_final.get("totalScore"),
            "findings": [{"severity": s, "title": t, "detail": d, "ref": ref}
                         for s, t, d, ref in findings],
        }, ensure_ascii=False, indent=2))
        return
    opp_name = m.opp_final.get("playerName") or m.opp_id
    print(f"\n{'=' * 64}")
    print(f"对局 {m.match_id}")
    print(f"我方 {m.me_id} vs {opp_name}({m.opp_id})")
    print("=" * 64)
    # 路线一览
    route = [seg[2] for seg in m.timeline if not seg[3]]
    dedup = [route[0]] if route else []
    for n in route[1:]:
        if n != dedup[-1]:
            dedup.append(n)
    print(f"我方路线: {'->'.join(dedup)}")
    print("-" * 64)
    for s, title, detail, ref in findings:
        print(f"{SEV[s]} {title}")
        if detail:
            print(f"    {detail}")
        if ref:
            print(f"    ↳ 参考 {ref}")
    print()


def summary_table(paths, me_id):
    rows = []
    for p in paths:
        try:
            first, rounds, last = load_replay(p)
            m = Match(first, rounds, last, me_id)
            findings = diagnose(m)
            crits = sum(1 for s, *_ in findings if s == "CRIT")
            me, opp = m.me_final, m.opp_final
            won = (me.get("totalScore") or 0) > (opp.get("totalScore") or 0)
            rows.append((os.path.basename(p),
                         opp.get("playerName") or m.opp_id,
                         f"{me.get('totalScore')}:{opp.get('totalScore')}",
                         "胜" if won else "负",
                         round((me.get("freshness") or 0) - (opp.get("freshness") or 0), 1),
                         (me.get("taskScore") or 0) - (opp.get("taskScore") or 0),
                         crits))
        except Exception as e:
            rows.append((os.path.basename(p), f"解析失败: {e}", "", "", "", "", ""))
    w = [max(len(str(r[i])) for r in rows + [("回放", "对手", "比分", "结果",
                                              "鲜度差", "任务差", "严重项")])
         for i in range(7)]
    hdr = ("回放", "对手", "比分", "结果", "鲜度差", "任务差", "严重项")
    print("  ".join(str(h).ljust(w[i]) for i, h in enumerate(hdr)))
    print("-" * (sum(w) + 12))
    for r in rows:
        print("  ".join(str(c).ljust(w[i]) for i, c in enumerate(r)))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="回放文件(.7z/.txt)或目录")
    ap.add_argument("--me", type=int, default=None, help="我方 playerId（默认自动识别）")
    ap.add_argument("--latest", action="store_true", help="目录模式：只分析最新一局")
    ap.add_argument("--all", action="store_true", help="目录模式：全部对局汇总表")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()

    if os.path.isdir(args.path):
        paths = find_replays(args.path)
        if not paths:
            sys.exit("目录里没有 replay 文件")
        if args.all:
            summary_table(paths, args.me)
            return
        targets = [paths[-1]] if args.latest else paths[-1:]
    else:
        targets = [args.path]

    for p in targets:
        first, rounds, last = load_replay(p)
        report(Match(first, rounds, last, args.me), as_json=args.json)


if __name__ == "__main__":
    main()

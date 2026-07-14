#!/usr/bin/env python3
"""Warden 本地场景哨兵：快速模拟 S10/S14 卡人策略。

平台排队慢时先跑这个脚本。它复用 arena.py 的离线裁判，专测
WardenStrategy 是否能做到：
- 我方按死线交付；
- 对手在极端冲刺/蹲点/边农边冲/官道农形态下被卡到未交付；
- 镜像 S02 长锁维持平局，不主动放弃。

用法：
    python3 client/warden_scenarios.py
    python3 client/warden_scenarios.py --seeds 0-9
    python3 client/warden_scenarios.py --json
"""
import argparse
import json
from multiprocessing import Pool
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from arena import PID_A, PID_B, run_match          # noqa: E402
from lychee.hybrid import HybridStrategy          # noqa: E402
from sparring import (CamperBot, RoadFarmerBot, RusherBot, TollerBot)  # noqa: E402


class FixedCamperBot(CamperBot):
    RANDOMIZE = False


class LateCamperBot(CamperBot):
    """死守型 camper：更晚离开，逼 Warden 的最后离场账本。"""
    RANDOMIZE = False
    LEAVE_ROUND = 520
    TASK_CAP = 240
    GUARD_DELAY = 0


class DelayedGuardCamperBot(CamperBot):
    """先农后卡型 camper：首卡延迟，测我们是否仍能抢 S10 所有权。"""
    RANDOMIZE = False
    LEAVE_ROUND = 430
    TASK_CAP = 180
    GUARD_DELAY = 120


class FixedTollerBot(TollerBot):
    RANDOMIZE = False


class AggressiveTollerBot(TollerBot):
    """边农边冲强化：更早 S13 二卡，更宽 ETA 回手卡窗口。"""
    RANDOMIZE = False
    GUARD_OPP_ETA = 220
    SECOND_GUARD_ROUND = 430
    TASK_CAP = 180


class GreedyRoadFarmerBot(RoadFarmerBot):
    """官道农强化：更贪任务、更久蹲富点。"""
    RANDOMIZE = False
    TASK_CAP = 180
    DWELL_MAX = 90
    LEAVE_ROUND = 450


class FastRoadFarmerBot(RoadFarmerBot):
    """官道农快交付：少蹲点，早点离场，测 S10 卡能否赶上。"""
    RANDOMIZE = False
    TASK_CAP = 120
    DWELL_MAX = 20
    LEAVE_ROUND = 370


BOT_CASES = {
    "rusher": RusherBot,
    "camper": CamperBot,
    "camper_fixed": FixedCamperBot,
    "camper_late": LateCamperBot,
    "camper_delayed": DelayedGuardCamperBot,
    "toller": TollerBot,
    "toller_fixed": FixedTollerBot,
    "toller_aggressive": AggressiveTollerBot,
    "roadfarmer": RoadFarmerBot,
    "roadfarmer_greedy": GreedyRoadFarmerBot,
    "roadfarmer_fast": FastRoadFarmerBot,
}

OURS = HybridStrategy


def parse_seeds(expr):
    if "-" in expr:
        lo, hi = expr.split("-", 1)
        return list(range(int(lo), int(hi) + 1))
    return [int(x) for x in expr.split(",") if x.strip()]


def summarize(kind, seed, seat, result):
    us_pid = PID_A if seat == "A" else PID_B
    opp_pid = PID_B if seat == "A" else PID_A
    us = result[us_pid]
    opp = result[opp_pid]
    if kind == "mirror":
        ok = (result["winner"] == 0 and not us["delivered"]
              and not opp["delivered"])
        goal = "S02长锁平局"
    else:
        ok = (result["winner"] == us_pid and us["delivered"]
              and not opp["delivered"])
        goal = "我方交付且对手未交付"
    margin = us["score"] - opp["score"]
    return {
        "kind": kind,
        "seed": seed,
        "seat": seat,
        "ok": ok,
        "goal": goal,
        "winner": result["winner"],
        "margin": margin,
        "us": {
            "score": us["score"],
            "delivered": us["delivered"],
            "deliverRound": us["deliverRound"],
            "taskBase": us["taskBase"],
            "good": us["good"],
            "fresh": us["fresh"],
            "guards": us["metrics"]["guards_set"],
            "forced": us["metrics"]["forced"],
        },
        "opp": {
            "score": opp["score"],
            "delivered": opp["delivered"],
            "deliverRound": opp["deliverRound"],
            "taskBase": opp["taskBase"],
            "frozen": opp["metrics"]["frozen_frames"],
            "guards": opp["metrics"]["guards_set"],
        },
}


def run_case(spec):
    kind, seed, seat = spec
    if kind == "mirror":
        result = run_match(seed, cls_a=OURS, cls_b=OURS)
        seat = "A"
    elif seat == "A":
        result = run_match(seed, cls_a=OURS, cls_b=BOT_CASES[kind])
    else:
        result = run_match(seed, cls_a=BOT_CASES[kind], cls_b=OURS)
    return summarize(kind, seed, seat, result)


def build_specs(kinds, seeds, seats):
    specs = []
    for kind in kinds:
        if kind == "mirror":
            specs.extend((kind, seed, "A") for seed in seeds)
            continue
        for seat in seats:
            specs.extend((kind, seed, seat) for seed in seeds)
    return specs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="0-5", help="如 0-9 或 0,3,7")
    ap.add_argument(
        "--kinds",
        default=("rusher,camper,camper_fixed,camper_late,camper_delayed,"
                 "toller,toller_fixed,toller_aggressive,roadfarmer,"
                 "roadfarmer_greedy,roadfarmer_fast,mirror"),
        help="逗号分隔；可选：" + ",".join((*BOT_CASES.keys(), "mirror")))
    ap.add_argument("--seats", default="A,B",
                    help="非 mirror 场景中 Warden 坐 A/B 哪些座位")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--fail-only", action="store_true",
                    help="只打印失败场景，适合大 seed 扫描")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    seeds = parse_seeds(args.seeds)
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    seats = [s.strip().upper() for s in args.seats.split(",") if s.strip()]
    specs = build_specs(kinds, seeds, seats)
    if args.jobs <= 1:
        rows = [run_case(spec) for spec in specs]
    else:
        with Pool(args.jobs) as pool:
            rows = pool.map(run_case, specs)

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        bad = 0
        for r in rows:
            mark = "PASS" if r["ok"] else "FAIL"
            if not r["ok"]:
                bad += 1
            elif args.fail_only:
                continue
            print(
                f"[{mark}] {r['kind']:18} seat={r['seat']} seed={r['seed']:>2} "
                f"margin={r['margin']:+4} "
                f"us={int(r['us']['delivered'])}@{r['us']['deliverRound']} "
                f"opp={int(r['opp']['delivered'])}@{r['opp']['deliverRound']} "
                f"task={r['us']['taskBase']}:{r['opp']['taskBase']} "
                f"guard={r['us']['guards']} frozen={r['opp']['frozen']} "
                f"goal={r['goal']}"
            )
        print(f"\n{'全绿' if bad == 0 else f'{bad} 个场景失败'}")
    return 1 if any(not r["ok"] for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())

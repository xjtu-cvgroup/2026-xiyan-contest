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
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from arena import PID_A, PID_B, run_match          # noqa: E402
from lychee.warden import WardenStrategy          # noqa: E402
from sparring import (CamperBot, RoadFarmerBot, RusherBot, TollerBot)  # noqa: E402


BOT_CASES = {
    "rusher": RusherBot,
    "camper": CamperBot,
    "toller": TollerBot,
    "roadfarmer": RoadFarmerBot,
}


def parse_seeds(expr):
    if "-" in expr:
        lo, hi = expr.split("-", 1)
        return list(range(int(lo), int(hi) + 1))
    return [int(x) for x in expr.split(",") if x.strip()]


def summarize(kind, seed, result):
    us = result[PID_A]
    opp = result[PID_B]
    if kind == "mirror":
        ok = (result["winner"] == 0 and not us["delivered"]
              and not opp["delivered"])
        goal = "S02长锁平局"
    else:
        ok = (result["winner"] == PID_A and us["delivered"]
              and not opp["delivered"])
        goal = "我方交付且对手未交付"
    return {
        "kind": kind,
        "seed": seed,
        "ok": ok,
        "goal": goal,
        "winner": result["winner"],
        "margin": result["margin"],
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


def run_case(kind, seed):
    if kind == "mirror":
        result = run_match(seed, cls_a=WardenStrategy, cls_b=WardenStrategy)
    else:
        result = run_match(seed, cls_a=WardenStrategy, cls_b=BOT_CASES[kind])
    return summarize(kind, seed, result)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="0-5", help="如 0-9 或 0,3,7")
    ap.add_argument("--kinds", default="rusher,camper,toller,roadfarmer,mirror",
                    help="逗号分隔：rusher,camper,toller,roadfarmer,mirror")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    seeds = parse_seeds(args.seeds)
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    rows = [run_case(kind, seed) for kind in kinds for seed in seeds]

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        bad = 0
        for r in rows:
            mark = "PASS" if r["ok"] else "FAIL"
            if not r["ok"]:
                bad += 1
            print(
                f"[{mark}] {r['kind']:10} seed={r['seed']:>2} "
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

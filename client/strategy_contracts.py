#!/usr/bin/env python3
"""核心策略的完整对局契约。

这不是随机胜率宣传，也不替代平台回放。它在离线裁判中连续推进 600 帧，
同时检查最终结果与关键时序，防止“单帧动作正确、整局状态机走歪”。
"""
import argparse
import sys

from arena import Arena, PID_A, PID_B
from lychee import protocol as P
from lychee.hybrid import HybridStrategy
from scenario_maps import e25_bypass_start, public_v42_start, variant1_start
from sparring import FarmerBot, RoadFarmerBot, RusherBot


FULL_OBSTACLES = ("S06", "S08", "S10", "S11")
VARIANT1_OBSTACLES = ("S06", "S07", "S10", "S11")


class GuardBreakerRusher(RusherBot):
    """冲锋同时用公开小分队拆卡，覆盖“墙会被反制”的连续链。"""

    def bot_squad(self, state):
        me = state.me
        nxt = me.get("nextNodeId")
        if me.get("routeEdgeId") and nxt and state.enemy_guard(nxt) \
                and (me.get("squadAvailable") or 0) >= 2:
            return P.a_squad_weaken(nxt)
        return None


WEATHER = {
    "clear": [],
    "rain": [{"type": "HEAVY_RAIN", "start": 1, "dur": 180}],
    "fog": [{"type": "MOUNTAIN_FOG", "start": 1, "dur": 180}],
    "hot": [{"type": "HOT", "start": 250, "dur": 180}],
}


CASES = (
    # 真主墙图：无障碍维持山线最快路；全障碍时含税路线切回 S02 水路。
    ("public-clear-rusher-A", public_v42_start, (), RusherBot, "A", "clear"),
    ("public-clear-rusher-B", public_v42_start, (), RusherBot, "B", "clear"),
    ("public-obstacles-rusher-A", public_v42_start, FULL_OBSTACLES,
     RusherBot, "A", "clear"),
    ("public-obstacles-rusher-B", public_v42_start, FULL_OBSTACLES,
     RusherBot, "B", "clear"),
    ("public-guard-breaker", public_v42_start, FULL_OBSTACLES,
     GuardBreakerRusher, "A", "clear"),

    # 用户提供的变种地图1：固定处理站语义和边长都与公开图不同。
    ("variant1-farmer-A", variant1_start, VARIANT1_OBSTACLES,
     RoadFarmerBot, "A", "clear"),
    ("variant1-farmer-B", variant1_start, VARIANT1_OBSTACLES,
     RoadFarmerBot, "B", "clear"),

    # replay.report.txt 的 E25 绕 S10 图：必须转 S14，而非在 S09 罚站。
    ("e25-rusher-A", e25_bypass_start, (), RusherBot, "A", "clear"),
    ("e25-rusher-B", e25_bypass_start, (), RusherBot, "B", "clear"),
    ("e25-farmer-A", e25_bypass_start, (), FarmerBot, "A", "clear"),
    ("e25-farmer-B", e25_bypass_start, (), FarmerBot, "B", "clear"),
    ("e25-roadfarmer-A", e25_bypass_start, (), RoadFarmerBot, "A", "clear"),
    ("e25-roadfarmer-B", e25_bypass_start, (), RoadFarmerBot, "B", "clear"),
    ("e25-rain", e25_bypass_start, (), RusherBot, "A", "rain"),
    ("e25-fog", e25_bypass_start, (), RusherBot, "A", "fog"),
    ("e25-hot", e25_bypass_start, (), RusherBot, "A", "hot"),
)


def _event_target(event):
    payload = event.get("payload") or {}
    return payload.get("targetNodeId") or payload.get("nodeId")


def _post_s02_waits(timeline, pid):
    completed = False
    waits = []
    for frame in timeline:
        if any(event.get("type") in ("PROCESS_COMPLETE", "PROCESS_COMPLETED")
               and (event.get("payload") or {}).get("playerId") == pid
               and _event_target(event) == "S02"
               for event in frame["events"]):
            completed = True
        if not completed:
            continue
        player = frame["players"][pid]
        if player["node"] != "S02" or player["edge"]:
            break
        main = frame["actions"][pid]["main"]
        if not main or main.get("action") == "WAIT":
            waits.append(frame["round"])
    return waits


def _max_s09_race_wait(timeline, us, opp):
    longest = current = 0
    for frame in timeline:
        mine = frame["players"][us]
        theirs = frame["players"][opp]
        main = frame["actions"][us]["main"]
        waiting = (mine["state"] == P.ST_IDLE
                   and mine["node"] == "S09" and not mine["edge"]
                   and bool(theirs["edge"])
                   and (not main or main.get("action") == "WAIT"))
        current = current + 1 if waiting else 0
        longest = max(longest, current)
    return longest


def run_case(spec, seed=0):
    name, map_factory, obstacles, bot, seat, weather = spec
    if seat == "A":
        cls_a, cls_b = HybridStrategy, bot
        us, opp = PID_A, PID_B
    else:
        cls_a, cls_b = bot, HybridStrategy
        us, opp = PID_B, PID_A
    arena = Arena(
        seed, cls_a=cls_a, cls_b=cls_b,
        start_data=map_factory(), obstacle_nodes=obstacles,
        weather_plan=WEATHER[weather], capture_timeline=True)
    result = arena.run()
    ours, theirs = result[us], result[opp]
    strategy = arena.strategies[us]
    failures = []

    if result["strategyErrors"]:
        failures.append(f"strategy errors={result['strategyErrors']}")
    if ours["illegal"]:
        failures.append(f"illegal={ours['illegal']}")
    if not ours["delivered"] or ours["deliverRound"] > 590:
        failures.append(f"delivery={ours['deliverRound']}")
    if ours["score"] <= theirs["score"]:
        failures.append(f"score={ours['score']}:{theirs['score']}")
    if theirs["delivered"]:
        failures.append(f"opponent delivered@{theirs['deliverRound']}")

    if name.startswith("public") or name.startswith("variant1"):
        if strategy.mode != HybridStrategy.MODE_PRIMARY \
                or strategy.primary_choke != "S10":
            failures.append(
                f"primary mode={strategy.mode} choke={strategy.primary_choke}")
    if name.startswith("e25"):
        if strategy.mode != HybridStrategy.MODE_GATE \
                or strategy.warden._forced_camp != "S14":
            failures.append(
                f"gate mode={strategy.mode} camp={strategy.warden._forced_camp}")
        waits = _post_s02_waits(result["timeline"], us)
        if waits:
            failures.append(f"wait after S02 process={waits}")
        s09_wait = _max_s09_race_wait(result["timeline"], us, opp)
        if s09_wait > 1:
            failures.append(f"S09 race wait streak={s09_wait}")

    return {
        "name": name,
        "ok": not failures,
        "failures": failures,
        "score": (ours["score"], theirs["score"]),
        "delivery": (ours["deliverRound"], theirs["deliverRound"]),
        "task": (ours["taskBase"], theirs["taskBase"]),
        "mode": strategy.mode,
    }


def run_mirror(seed=1):
    arena = Arena(
        seed, cls_a=HybridStrategy, cls_b=HybridStrategy,
        start_data=variant1_start(), obstacle_nodes=VARIANT1_OBSTACLES,
        weather_plan=[], capture_timeline=True)
    result = arena.run()
    ok = (result["winner"] == 0
          and not result[PID_A]["delivered"]
          and not result[PID_B]["delivered"]
          and not result["strategyErrors"]
          and result[PID_A]["illegal"] == 0
          and result[PID_B]["illegal"] == 0)
    return {
        "name": "variant1-mirror-no-concession",
        "ok": ok,
        "failures": [] if ok else [
            f"winner={result['winner']} "
            f"delivery={result[PID_A]['deliverRound']}:"
            f"{result[PID_B]['deliverRound']}"],
        "score": (result[PID_A]["score"], result[PID_B]["score"]),
        "delivery": (result[PID_A]["deliverRound"],
                     result[PID_B]["deliverRound"]),
        "task": (result[PID_A]["taskBase"], result[PID_B]["taskBase"]),
        "mode": "mirror",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    rows = [run_case(case, args.seed) for case in CASES]
    rows.append(run_mirror(args.seed + 1))
    for row in rows:
        mark = "PASS" if row["ok"] else "FAIL"
        print(
            f"[{mark}] {row['name']:28} "
            f"score={row['score'][0]}:{row['score'][1]} "
            f"delivery={row['delivery'][0]}:{row['delivery'][1]} "
            f"task={row['task'][0]}:{row['task'][1]} mode={row['mode']}"
        )
        for failure in row["failures"]:
            print(f"       {failure}")
    failed = sum(not row["ok"] for row in rows)
    print(f"\nstrategy contracts: {len(rows) - failed}/{len(rows)} PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

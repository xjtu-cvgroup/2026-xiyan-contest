#!/usr/bin/env python3
"""Hybrid 动态卡点快速结构化鲁棒性检查（不跑对局电池）。

随机扰动地图旁路、边长、天气、边进度与双方资源，验证计划数学不变量。
该脚本只读策略输出，不参与线上行为。
"""
import argparse
import copy
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lychee import protocol as P                 # noqa: E402
from lychee.hybrid import HybridStrategy         # noqa: E402
from lychee.state import GameState               # noqa: E402


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
START_FILE = os.path.join(ROOT, "start消息.json")
INQUIRE_FILE = os.path.join(ROOT, "inquire消息.json")


def _load_fixture(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)["msg_data"]


BASE_START = _load_fixture(START_FILE)
BASE_INQUIRE = _load_fixture(INQUIRE_FILE)


def _variant_start(rng, index):
    start = copy.deepcopy(BASE_START)
    nodes = [n["nodeId"] for n in start["nodes"]]
    edges = start["edges"]

    for edge in edges:
        edge["distance"] = max(
            2, round((edge.get("distance") or 10) * rng.uniform(0.65, 1.55)))

    existing = {
        frozenset((e.get("fromNodeId"), e.get("toNodeId"))) for e in edges
    }
    for n in range(rng.randint(0, 3)):
        for _ in range(30):
            src, dst = rng.sample(nodes, 2)
            pair = frozenset((src, dst))
            if pair not in existing:
                break
        else:
            continue
        existing.add(pair)
        edges.append({
            "edgeId": f"E_FUZZ_{index}_{n}",
            "fromNodeId": src,
            "toNodeId": dst,
            "routeType": rng.choice((P.ROAD, P.WATER, P.MOUNTAIN, P.BRANCH)),
            "distance": rng.randint(5, 90),
            "bidirectional": True,
        })
    return start


def _build_state(rng, index):
    start = _variant_start(rng, index)
    state = GameState(1001)
    state.on_start(start)

    directed_edges = [
        (src, dst, edge)
        for src in state.graph.adj
        for dst, edge in state.graph.neighbors(src)
        if dst not in (state.start_node,)
    ]
    opp_cur, opp_next, opp_edge = rng.choice(directed_edges)
    total = state.graph.edge_total_move(opp_edge)
    progress = rng.randint(0, max(0, int(total * 0.85)))

    node_ids = list(state.static_nodes)
    my_cur = rng.choice(node_ids)
    round_no = rng.randint(70, 545)

    inquire = copy.deepcopy(BASE_INQUIRE)
    inquire.update({
        "round": round_no,
        "phase": P.PHASE_RUSH if round_no >= 390 else P.PHASE_NORMAL,
        "edges": start["edges"],
        "tasks": [], "contests": [], "events": [], "actionResults": [],
    })
    if rng.random() < 0.35:
        inquire["weather"] = {"active": [{
            "type": rng.choice((P.HEAVY_RAIN, P.MOUNTAIN_FOG)),
            "remainRound": rng.randint(5, 80),
        }], "forecast": []}
    else:
        inquire["weather"] = {"active": [], "forecast": []}

    for player in inquire["players"]:
        if player["playerId"] == state.player_id:
            player.update(
                state=P.ST_IDLE, currentNodeId=my_cur, nextNodeId=None,
                routeEdgeId=None, currentProcess=None, buffs=[], resources={},
                freshness=rng.uniform(55, 100), goodFruit=rng.randint(0, 100),
                badFruit=rng.randint(0, 6), taskScore=rng.randrange(0, 181, 15),
                squadAvailable=rng.choice((0, 2, 4, 6, 8)),
                guardActionPoint=rng.randint(0, 4), rushTacticUsedCount=0,
                verified=False, delivered=False, retired=False)
        else:
            player.update(
                state=P.ST_MOVING, currentNodeId=opp_cur,
                nextNodeId=opp_next, routeEdgeId=opp_edge["edgeId"],
                edgeTotalMs=total, edgeProgressMs=progress,
                currentProcess=None, buffs=[], resources={},
                freshness=rng.uniform(55, 100), goodFruit=rng.randint(0, 100),
                badFruit=rng.randint(0, 6), taskScore=rng.randrange(0, 181, 15),
                squadAvailable=rng.choice((0, 2, 4, 6, 8, None)),
                verified=False, delivered=False, retired=False)
    for node in inquire["nodes"]:
        node.update(hasObstacle=False, guard=None, resourceStock={}, scouted=[])
    state.on_inquire(inquire)
    return state


def _check_plan(state, strategy, plan):
    remain = state.duration_round - state.round
    assert plan["target"] not in (
        state.start_node, "S02", state.gate_node, state.terminal_node)
    assert plan["myEta"] + strategy.warden.MOBILE_GUARD_PAD \
        <= plan["oppEta"]
    assert plan["delay"] == min(plan["stayDelay"], plan["rerouteDelay"])
    assert plan["delay"] >= strategy.warden.MOBILE_GUARD_MIN_DELAY
    assert plan["myFinish"] + strategy.warden.EXIT_PAD <= remain
    assert plan["finishTax"] == max(
        0, plan["oppFinish"] - plan["oppBaseFinish"])
    expected_mandatory = not strategy._reachable_without(
        state, state.start_node, state.gate_node, plan["target"])
    assert plan["globallyMandatory"] == expected_mandatory
    if plan["denial"]:
        assert plan["oppBaseFinish"] <= remain < plan["oppFinish"]
    else:
        assert plan["detour"] <= strategy.MOBILE_APPROACH_MAX_DETOUR
        assert state.me.get("goodFruit", 0) - plan["guardCost"] \
            >= strategy.warden.FRUIT_RESERVE


def run(cases, seed):
    rng = random.Random(seed)
    stats = {"cases": cases, "plans": 0, "denials": 0,
             "bypassPlans": 0, "mandatoryPlans": 0}
    for index in range(cases):
        state = _build_state(rng, index)
        strategy = HybridStrategy()
        strategy.mode = strategy.MODE_MOBILE
        plan = strategy._mobile_control_plan(state)
        if state.me.get("currentNodeId") in (
                state.start_node, "S02", state.gate_node,
                state.terminal_node):
            assert plan is None, (index, state.me.get("currentNodeId"), plan)
        actions = strategy.decide(state)
        mains = [a for a in actions if a.get("action") in P.MAIN_ACTION_TYPES]
        assert len(mains) <= 1, (index, actions)
        if not plan:
            continue
        try:
            _check_plan(state, strategy, plan)
        except AssertionError as exc:
            raise AssertionError(
                f"case={index} round={state.round} plan={plan}") from exc
        stats["plans"] += 1
        stats["denials"] += int(plan["denial"])
        key = "mandatoryPlans" if plan["globallyMandatory"] else "bypassPlans"
        stats[key] += 1
    return stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=int, default=400)
    parser.add_argument("--seed", type=int, default=20260714)
    args = parser.parse_args()
    stats = run(args.cases, args.seed)
    print("Hybrid robustness PASS " + " ".join(
        f"{key}={value}" for key, value in stats.items()))


if __name__ == "__main__":
    main()

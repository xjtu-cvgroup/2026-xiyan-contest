#!/usr/bin/env python3
"""Hybrid 快速结构化检查（不跑完整对局电池）。

两组边界彼此独立：
1. 随机扰动地图旁路、边长、天气、边进度与资源，验证动态卡点公式；
2. 专门覆盖 r40-r69 的 S02 状态，逐动作对照 3.96.34 Warden。

这仍不是胜率模拟，输出只证明列出的结构化契约，不代表“随机对局全通过”。
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
from lychee.warden import WardenStrategy          # noqa: E402


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

    # S14 仍为终点前必经点，不能随机制造直达 S15 的非法旁路。
    roles = start["map"]["gameplay"]["roles"]
    terminal = (roles.get("terminalNodeIds") or ["S15"])[0]
    extra_nodes = [node_id for node_id in nodes if node_id != terminal]
    existing = {
        frozenset((e.get("fromNodeId"), e.get("toNodeId"))) for e in edges
    }
    for n in range(rng.randint(0, 3)):
        for _ in range(30):
            src, dst = rng.sample(extra_nodes, 2)
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
            "bidirectional": rng.random() >= 0.25,
        })

    gameplay = start["map"]["gameplay"]
    for resource in gameplay.get("resources") or []:
        resource["claimRound"] = rng.randint(1, 8)
    for proc in gameplay.get("processNodes") or []:
        proc["processRound"] = rng.randint(2, 9)
        for node in start["nodes"]:
            if node["nodeId"] == proc["nodeId"]:
                node["processRound"] = proc["processRound"]
                node["processType"] = proc.get("processType")
                break
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
    if round_no >= 450:
        phase = P.PHASE_RUSH
    elif round_no >= 390 and rng.random() < 0.55:
        phase = P.PHASE_RUSH
    else:
        phase = P.PHASE_NORMAL
    inquire.update({
        "round": round_no,
        "phase": phase,
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

    speed_resources = {
        P.FAST_HORSE: rng.randint(0, 2),
        P.SHORT_HORSE: rng.randint(0, 2),
    }
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
                currentProcess=None, buffs=[], resources=speed_resources,
                freshness=rng.uniform(55, 100), goodFruit=rng.randint(0, 100),
                badFruit=rng.randint(0, 6), taskScore=rng.randrange(0, 181, 15),
                squadAvailable=rng.choice((0, 2, 4, 6, 8, None)),
                verified=False, delivered=False, retired=False)
    for node in inquire["nodes"]:
        stock = {}
        if rng.random() < 0.12:
            stock[rng.choice((P.FAST_HORSE, P.SHORT_HORSE))] = rng.randint(1, 2)
        node.update(hasObstacle=False, guard=None,
                    resourceStock=stock, scouted=[])
    state.on_inquire(inquire)
    return state


def _s02_fixture(rng, index):
    """生成早期 S02 动作快照；刻意覆盖旧脚本完全跳过的 r40-r69。"""
    start = _variant_start(rng, 1000000 + index)
    # 明确制造 S10 旁路，保证测试走 Hybrid 的 MOBILE 集成路径。
    start["edges"].append({
        "edgeId": f"E_S02_BYPASS_{index}",
        "fromNodeId": "S09", "toNodeId": "S11",
        "routeType": P.BRANCH, "distance": 24, "bidirectional": True,
    })
    inquire = copy.deepcopy(BASE_INQUIRE)
    round_no = rng.randint(40, 69)
    my_state = rng.choice((P.ST_IDLE, P.ST_CONTESTING,
                           P.ST_RESTING, P.ST_PROCESSING))
    inquire.update({
        "round": round_no, "phase": P.PHASE_NORMAL,
        "edges": start["edges"], "tasks": [], "events": [],
        "actionResults": [], "weather": {"active": [], "forecast": []},
    })
    inquire["contests"] = []
    if my_state == P.ST_CONTESTING:
        inquire["contests"] = [{
            "contestId": f"C_S02_FUZZ_{index}",
            "contestType": P.CONTEST_DOCK, "targetNodeId": "S02",
            "redPlayerId": 1001, "bluePlayerId": 2002,
            "redPoint": rng.randint(0, 1), "bluePoint": rng.randint(0, 1),
            "resolved": False,
        }]

    opp_mode = rng.choice(("idle", "inbound", "processing", "departed"))
    for player in inquire["players"]:
        if player["playerId"] == 1001:
            process = None
            if my_state == P.ST_PROCESSING:
                process = {"action": "PROCESS", "type": "PROCESS",
                           "targetNodeId": "S02", "remainRound": 2}
            player.update(
                state=my_state, currentNodeId="S02", nextNodeId=None,
                routeEdgeId=None, currentProcess=process, buffs=[],
                resources={}, freshness=rng.uniform(78.0, 100.0),
                goodFruit=rng.randint(0, 100), badFruit=rng.randint(0, 2),
                taskScore=0, squadAvailable=rng.choice((0, 2, 4, 6, 8)),
                guardActionPoint=rng.randint(0, 4), rushTacticUsedCount=0,
                verified=False, delivered=False, retired=False)
            continue

        player.update(
            state=P.ST_IDLE, currentNodeId="S02", nextNodeId=None,
            routeEdgeId=None, currentProcess=None, buffs=[], resources={},
            freshness=95.0, goodFruit=90, badFruit=0, taskScore=0,
            squadAvailable=8, verified=False, delivered=False, retired=False)
        if opp_mode == "inbound":
            player.update(
                state=P.ST_MOVING, currentNodeId="S01", nextNodeId="S02",
                routeEdgeId="E01", edgeTotalMs=42780,
                edgeProgressMs=rng.randint(0, 42000))
        elif opp_mode == "processing":
            player.update(
                state=P.ST_PROCESSING,
                currentProcess={"action": "PROCESS", "type": "PROCESS",
                                "targetNodeId": "S02", "remainRound": 2})
        elif opp_mode == "departed":
            player.update(currentNodeId=rng.choice(("S03", "S04")))

    for node in inquire["nodes"]:
        node["guard"] = None
        if node["nodeId"] == "S02":
            node.update(processType="TRANSFER", processRound=4,
                        hasObstacle=False)
    return start, inquire


def _state_from(start, inquire):
    state = GameState(1001)
    state.on_start(copy.deepcopy(start))
    state.on_inquire(copy.deepcopy(inquire))
    return state


def _check_s02_differential(cases, seed):
    """MOBILE 集成层在 S02 未完成时必须与 Warden 逐动作相等。"""
    rng = random.Random(seed ^ 0x39634)
    for index in range(cases):
        start, inquire = _s02_fixture(rng, index)
        state_h = _state_from(start, inquire)
        state_w = _state_from(start, inquire)
        hybrid = HybridStrategy()
        hybrid.on_start(state_h)
        hybrid.mode = HybridStrategy.MODE_MOBILE
        reference = WardenStrategy()
        reference.on_start(state_w)
        actions_h = hybrid.decide(state_h)
        actions_w = reference.decide(state_w)
        assert actions_h == actions_w, (
            index, state_h.round, state_h.me.get("state"), actions_h, actions_w)
        assert not any(a.get("action") == "SET_GUARD" for a in actions_h), (
            index, actions_h)
        assert not any(
            a.get("action") == "SQUAD_SCOUT"
            and a.get("targetNodeId") in ("S03", "S04")
            for a in actions_h), (index, actions_h)
    return cases


def _check_plan(state, strategy, plan):
    remain = state.duration_round - state.round
    assert plan["target"] not in (
        state.start_node, "S02", state.gate_node, state.terminal_node)
    assert plan["myEta"] + strategy.warden.MOBILE_GUARD_PAD \
        <= plan["oppEta"]
    assert plan["delay"] == min(
        plan["stayDelay"], plan["rerouteDelay"], plan["reentryDelay"])
    assert plan["delay"] >= strategy.warden.MOBILE_GUARD_MIN_DELAY \
        or (plan.get("upstreamContract")
            and plan["stayDelay"] >= strategy.warden.MOBILE_GUARD_MIN_DELAY)
    assert plan["myFinish"] + strategy.warden.EXIT_PAD \
        + strategy.warden.MOBILE_GUARD_PAD <= remain
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
             "bypassPlans": 0, "mandatoryPlans": 0,
             "s02Differential": _check_s02_differential(
                 max(200, min(cases, 2000)), seed)}
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
    print("Hybrid structural contracts PASS " + " ".join(
        f"{key}={value}" for key, value in stats.items()))


if __name__ == "__main__":
    main()

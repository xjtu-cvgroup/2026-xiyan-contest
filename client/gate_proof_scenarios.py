#!/usr/bin/env python3
"""宫门先手证明的快速规则场景。

这些用例不跑完整对局，只核对“始终保有设卡先手”所依赖的硬账本：
路线公式、有限增益、天气、固定处理、阻挡、反应窗和交付余量。
"""
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from arena import Arena, PID_A, PID_B
from lychee import protocol as P
from lychee.hybrid import HybridStrategy
from lychee.planner import Plan
from lychee.state import GameState
from lychee.warden import WardenStrategy
from scenario_maps import (e25_bypass_start, gate_bypass_start, public_v42_start,
                           variant1_start)


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    print(f"[PASS] {name}" + (f"  {detail}" if detail else ""))


def make_state(start=None, round_no=1, phase=P.PHASE_NORMAL,
               my_node="S09", opp_node="S02", weather=None,
               my_buffs=None, obstacle_nodes=(), enemy_guards=(),
               my_edge=None, my_edge_progress=0):
    """从平台 start 结构构造最小但完整的公开状态。"""
    start = json.loads(json.dumps(start or public_v42_start()))
    state = GameState(PID_A)
    state.on_start(start)
    nodes = []
    for static in start.get("nodes") or start["map"]["nodes"]:
        node = dict(static)
        node["hasObstacle"] = node["nodeId"] in obstacle_nodes
        node["resourceStock"] = {}
        node["scouted"] = []
        node["guard"] = None
        if node["nodeId"] in enemy_guards:
            node["guard"] = {
                "ownerTeamId": "BLUE", "defense": 4, "active": True,
            }
        nodes.append(node)

    def player(pid, team, node_id):
        return {
            "playerId": pid, "teamId": team, "state": P.ST_IDLE,
            "currentNodeId": node_id, "nextNodeId": None,
            "routeEdgeId": None, "edgeTotalMs": 0, "edgeProgressMs": 0,
            "currentProcess": None, "buffs": [], "resources": {},
            "goodFruit": 80, "badFruit": 1, "freshness": 90.0,
            "taskScore": 120, "totalScore": 120, "squadAvailable": 8,
            "guardActionPoint": 4, "rushTacticUsedCount": 0,
            "verified": False, "delivered": False, "retired": False,
        }

    me = player(PID_A, "RED", my_node)
    opp = player(PID_B, "BLUE", opp_node)
    me["buffs"] = list(my_buffs or [])
    if my_edge:
        edge_id, edge_from, edge_to = my_edge
        edge = state.graph.edges[edge_id]
        me.update(
            state=P.ST_MOVING, currentNodeId=None, nextNodeId=edge_to,
            routeEdgeId=edge_id,
            edgeTotalMs=state.graph.edge_total_move(edge),
            edgeProgressMs=my_edge_progress,
        )
        # The service exposes the route start only indirectly. The tuple keeps
        # the fixture readable and validates that the chosen edge is legal.
        assert state.graph.edge_between(edge_from, edge_to) is edge

    state.on_inquire({
        "matchId": start["matchId"], "round": round_no, "phase": phase,
        "players": [me, opp], "nodes": nodes, "edges": start["edges"],
        "weather": weather or {"active": [], "forecast": []},
        "tasks": [], "bounties": [], "contests": [], "events": [],
        "actionResults": [], "scorePreview": {},
    })
    return state


def oracle_edge_frames(total_move, route_type, start_round, weather_at,
                       boost_speed=P.BASE_SPEED, boost_frames=0):
    """只按任务书 2.3.2 写的逐帧独立计算器。"""
    moved = frames = 0
    while moved < total_move:
        speed = boost_speed if frames < boost_frames else P.BASE_SPEED
        weather = weather_at(start_round + frames)
        tax = 1000
        if weather == P.HEAVY_RAIN and route_type == P.WATER:
            tax = 1350
        elif weather == P.MOUNTAIN_FOG and route_type == P.MOUNTAIN:
            tax = 1100
        moved += math.floor(speed * 1000 / tax)
        frames += 1
    return frames


def test_route_and_weather_formula():
    state = make_state(round_no=100, weather={
        "active": [{"type": P.HEAVY_RAIN, "remainRound": 60}],
        "forecast": [],
    })
    warden = WardenStrategy()
    water = next(e for e in state.graph.edges.values()
                 if e.get("routeType") == P.WATER)
    total = state.graph.edge_total_move(water)
    expected = oracle_edge_frames(
        total, P.WATER, 100,
        lambda rnd: P.HEAVY_RAIN if rnd < 160 else None)
    actual, _, _ = warden._edge_dynamic_frames(
        state, water, 0, None, 0, conservative_weather=True)
    check("暴雨水路逐帧公式", actual == expected,
          f"expected={expected} actual={actual}")

    mountain = next(e for e in state.graph.edges.values()
                    if e.get("routeType") == P.MOUNTAIN)
    total = state.graph.edge_total_move(mountain)
    expected = oracle_edge_frames(
        total, P.MOUNTAIN, 100, lambda _: None,
        P.SPEED_FAST_HORSE, 3)
    actual, boost, remain = warden._edge_dynamic_frames(
        state, mountain, 0, P.FAST_HORSE, 3,
        conservative_weather=False)
    check("快马仅覆盖剩余3帧", actual == expected and not boost and remain == 0,
          f"expected={expected} actual={actual}")

    unknown = make_state(round_no=70)
    clear, _ = warden._travel_dynamic(
        unknown, "S02", "S05", conservative_weather=True)
    worst, _ = warden._travel_dynamic(
        unknown, "S02", "S05", conservative_weather=True,
        worst_unknown_weather=True)
    check("未预告天气对水路采用规则最坏包络", worst >= clear,
          f"clear={clear} worst={worst}")

    known_hot = make_state(round_no=70, weather={
        "active": [],
        "forecast": [{"type": P.HOT, "startRound": 90,
                      "durationRound": 60}],
    })
    exact, _ = warden._travel_dynamic(
        known_hot, "S02", "S05", conservative_weather=True)
    bounded, _ = warden._travel_dynamic(
        known_hot, "S02", "S05", conservative_weather=True,
        worst_unknown_weather=True)
    check("本天气窗已预告后不重复虚构暴雨", exact == bounded,
          f"exact={exact} bounded={bounded}")


def test_processing_rules():
    rainy = make_state(variant1_start(), round_no=100, weather={
        "active": [{"type": P.HEAVY_RAIN, "remainRound": 60}],
        "forecast": [],
    })
    warden = WardenStrategy()
    s04 = rainy.node("S04")
    base = int(s04.get("processRound") or 0)
    actual = warden._node_process_frames_at(rainy, "S04", 100)
    check("暴雨登船/换运固定处理加4帧", actual == base + 4,
          f"base={base} actual={actual}")

    dry = make_state(variant1_start(), round_no=70)
    normal = warden._node_process_frames_at(dry, "S04", 90)
    worst = warden._node_process_frames_at(
        dry, "S04", 90, worst_unknown_weather=True)
    check("未预告天气覆盖固定水路处理", worst == normal + 4,
          f"normal={normal} worst={worst}")

    arena = Arena(1, start_data=variant1_start(), weather_plan=[
        {"type": P.HEAVY_RAIN, "start": 80, "dur": 60},
    ], obstacle_nodes=[])
    arena.round = 100
    check("本地裁判同步暴雨处理规则",
          arena._proc_frames(PID_A, "S04", base) == base + 4)


def test_current_edge_and_public_blocks():
    base = make_state(round_no=100)
    edge = next(e for e in base.graph.edges.values()
                if e.get("routeType") == P.WATER)
    frm, to = edge["fromNodeId"], edge["toNodeId"]
    progress = max(1, base.graph.edge_total_move(edge) // 3)
    state = make_state(
        round_no=100,
        weather={"active": [{"type": P.HEAVY_RAIN, "remainRound": 60}],
                 "forecast": []},
        my_edge=(edge["edgeId"], frm, to), my_edge_progress=progress,
        my_buffs=[{"type": P.FAST_HORSE, "remainRound": 2}],
    )
    hybrid = HybridStrategy()
    expected = oracle_edge_frames(
        state.me["edgeTotalMs"] - progress, P.WATER, 100,
        lambda rnd: P.HEAVY_RAIN if rnd < 160 else None,
        P.SPEED_FAST_HORSE, 2)
    actual, _, _ = hybrid._conservative_edge_remaining(
        state, state.me, P.FAST_HORSE, 2)
    check("在途余量同时计天气和增益耗尽", actual == expected,
          f"expected={expected} actual={actual}")

    open_state = make_state(my_node="S09", opp_node="S02")
    obstacle = make_state(my_node="S09", opp_node="S02",
                          obstacle_nodes=("S10",))
    guard = make_state(my_node="S09", opp_node="S02",
                       enemy_guards=("S10",))
    open_eta = hybrid._gate_eta(open_state, open_state.me)
    check("无阻挡时宫门ETA可达", open_eta < 999, f"eta={open_eta}")
    check("保证型ETA不穿越公开障碍",
          hybrid._gate_eta(obstacle, obstacle.me) == 999)
    check("保证型ETA不穿越敌方有效设卡",
          hybrid._gate_eta(guard, guard.me) == 999)
    check("对手下界仍按无阻挡极限速度",
          hybrid._gate_eta(guard, guard.opp, optimistic=True) < 999)


def test_task_budget_and_gate_topology():
    state = make_state(variant1_start(), round_no=70,
                       my_node="S02", opp_node="S01")
    task = {"taskId": "T_FIXED", "nodeId": "S04", "processRound": 4}
    plan = Plan("task", task=task, position="S04")
    hybrid = HybridStrategy()
    hybrid.planner._processed_here = True
    cost = hybrid._gate_plan_opportunity_cost(state, plan)

    changed = variant1_start()
    for node in changed["nodes"]:
        if node["nodeId"] == "S04":
            node.pop("processType", None)
            node["processRound"] = 0
    for proc in changed["map"]["gameplay"].get("processNodes") or []:
        if proc["nodeId"] == "S04":
            proc["processType"] = None
            proc["processRound"] = 0
    no_fixed = make_state(changed, round_no=70,
                          my_node="S02", opp_node="S01")
    hybrid2 = HybridStrategy()
    hybrid2.planner._processed_here = True
    cheaper = hybrid2._gate_plan_opportunity_cost(no_fixed, plan)
    check("任务绕行成本包含目标站固定处理", cost > cheaper,
          f"with={cost} without={cheaper}")

    public = make_state(public_v42_start(), my_node="S13", opp_node="S12")
    bypass = make_state(gate_bypass_start(), my_node="S13", opp_node="S12")
    check("公开图S14具备规则反应窗", hybrid._gate_has_reaction_window(public))
    check("终点旁路存在时禁用S14必赢证明",
          not hybrid._gate_has_reaction_window(bypass))

    short = public_v42_start()
    for edge in short["edges"]:
        if "S14" in (edge.get("fromNodeId"), edge.get("toNodeId")) \
                and "S15" not in (edge.get("fromNodeId"),
                                  edge.get("toNodeId")):
            edge["distance"] = 3
    short["map"]["edges"] = json.loads(json.dumps(short["edges"]))
    four = make_state(short, my_node="S13", opp_node="S12")
    check("最快入边不足5帧时拒绝设卡证明",
          not hybrid._gate_has_reaction_window(four))

    exact = public_v42_start()
    for edge in exact["edges"]:
        if "S14" in (edge.get("fromNodeId"), edge.get("toNodeId")) \
                and "S15" not in (edge.get("fromNodeId"),
                                  edge.get("toNodeId")):
            edge["routeType"] = P.BRANCH
            edge["distance"] = 4  # ceil(4 * 1550 / 1300) == 5
    exact["map"]["edges"] = json.loads(json.dumps(exact["edges"]))
    five = make_state(exact, my_node="S13", opp_node="S12")
    check("最快入边恰好5帧时允许设卡证明",
          hybrid._gate_has_reaction_window(five))


def test_mobile_intercept_uses_conservative_eta():
    state = make_state(
        e25_bypass_start(distance=24), round_no=100,
        my_node="S10", opp_node="S05",
        my_buffs=[{"type": P.FAST_HORSE, "remainRound": 1}],
    )
    edge = state.graph.edges["E19"]
    state.opp.update(
        state=P.ST_MOVING, currentNodeId="S05", nextNodeId="S09",
        routeEdgeId="E19", edgeTotalMs=state.graph.edge_total_move(edge),
        edgeProgressMs=0,
    )
    hybrid = HybridStrategy()
    plan = hybrid._mobile_control_plan(state)
    check("2621场景能找到动态汇合点", bool(plan), str(plan))
    expected, path = hybrid.warden._travel_dynamic(
        state, "S10", plan["target"], P.FAST_HORSE, 1,
        include_intermediate_process=True, conservative_weather=True,
        **hybrid._gate_travel_kwargs(state))
    full_horse, _ = hybrid.warden._shortest(
        state, "S10", plan["target"], P.SPEED_FAST_HORSE)
    check("2621先到判定不把1帧快马套满全程",
          plan["target"] != "S10" and plan["myEta"] == expected
          and expected > full_horse,
          f"target={plan['target']} plan={plan['myEta']} "
          f"expected={expected} old={full_horse} path={path}")


def test_finish_buffer_boundary():
    probe = make_state(round_no=400, phase=P.PHASE_RUSH,
                       my_node="S13", opp_node="S09")
    hybrid = HybridStrategy()
    hybrid.planner._processed_here = True
    my_eta = hybrid._gate_eta(probe, probe.me)
    need = hybrid._my_finish_need(probe, my_eta)
    pad = hybrid.warden.EXIT_PAD

    safe_round = probe.duration_round - need - pad
    safe = make_state(round_no=safe_round, phase=P.PHASE_RUSH,
                      my_node="S13", opp_node="S09")
    safe.opp["taskScore"] = 120
    hybrid_safe = HybridStrategy()
    hybrid_safe.planner._processed_here = True
    check("交付余量恰含10帧时仍可接管宫门",
          hybrid_safe._should_commit_gate(safe),
          f"round={safe_round} need={need} pad={pad}")

    late = make_state(round_no=safe_round + 1, phase=P.PHASE_RUSH,
                      my_node="S13", opp_node="S09")
    hybrid_late = HybridStrategy()
    hybrid_late.planner._processed_here = True
    check("少1帧交付余量时拒绝继续堵人",
          not hybrid_late._should_commit_gate(late),
          f"round={safe_round + 1}")


def main():
    test_route_and_weather_formula()
    test_processing_rules()
    test_current_edge_and_public_blocks()
    test_task_budget_and_gate_topology()
    test_mobile_intercept_uses_conservative_eta()
    test_finish_buffer_boundary()
    print("ALL GATE PROOF SCENARIOS PASS")


if __name__ == "__main__":
    main()

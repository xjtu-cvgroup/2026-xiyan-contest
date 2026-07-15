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
from scenario_maps import (e25_bypass_start, gate_bypass_start,
                           predicted_optional_s02_start,
                           predicted_split_corridors_start,
                           predicted_short_gate_start, public_v42_start,
                           variant1_e25_start, variant1_start)


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    print(f"[PASS] {name}" + (f"  {detail}" if detail else ""))


def make_state(start=None, round_no=1, phase=P.PHASE_NORMAL,
               my_node="S09", opp_node="S02", weather=None,
               my_buffs=None, obstacle_nodes=(), enemy_guards=(),
               my_edge=None, my_edge_progress=0,
               opp_edge=None, opp_edge_progress=0):
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
    def put_on_edge(player_data, edge_spec, progress):
        edge_id, edge_from, edge_to = edge_spec
        edge = state.graph.edges[edge_id]
        player_data.update(
            state=P.ST_MOVING, currentNodeId=edge_from, nextNodeId=edge_to,
            routeEdgeId=edge_id,
            edgeTotalMs=state.graph.edge_total_move(edge),
            edgeProgressMs=progress,
        )
        assert state.graph.edge_between(edge_from, edge_to) is edge

    if my_edge:
        put_on_edge(me, my_edge, my_edge_progress)
    if opp_edge:
        put_on_edge(opp, opp_edge, opp_edge_progress)

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

    configurable = variant1_start()
    for proc in configurable["map"]["gameplay"].get("processNodes") or []:
        if proc.get("nodeId") == "S14":
            proc["processRound"] = 9
    for node in configurable.get("nodes") or []:
        if node.get("nodeId") == "S14":
            node["processRound"] = 9
    for resource in configurable["map"]["gameplay"].get("resources") or []:
        if resource.get("nodeId") == "S03" \
                and resource.get("resourceType") == P.ICE_BOX:
            resource["claimRound"] = 7
    dynamic = Arena(2, start_data=configurable, weather_plan=[],
                    obstacle_nodes=[])
    dynamic._start_pending(PID_A, {"kind": "VERIFY_GATE"})
    check("本地裁判宫门验核读取地图9帧",
          dynamic.teams[PID_A].proc["remain"] == 9)
    dynamic.teams[PID_A].proc = None
    dynamic._start_pending(PID_A, {
        "kind": "CLAIM_RESOURCE", "node": "S03", "rtype": P.ICE_BOX,
    })
    check("本地裁判资源领取读取地图7帧",
          dynamic.teams[PID_A].proc["remain"] == 7)

    gate_nine = make_state(configurable, round_no=120,
                           my_node="S09", opp_node="S02")
    gate_six = make_state(variant1_start(), round_no=120,
                          my_node="S09", opp_node="S02")
    plan_nine = HybridStrategy().planner.planner.plan(gate_nine)
    plan_six = HybridStrategy().planner.planner.plan(gate_six)
    check("Planner交付余量读取宫门9帧而非固定6帧",
          plan_nine.slack == plan_six.slack - 3,
          f"nine={plan_nine.slack} six={plan_six.slack}")

    long_horse = make_state(configurable, round_no=120,
                            my_node="S03", opp_node="S02")
    long_horse.nodes["S03"]["resourceStock"] = {P.FAST_HORSE: 1}
    long_horse.resource_config = [
        item for item in long_horse.resource_config
        if not (item.get("nodeId") == "S03"
                and item.get("resourceType") == P.FAST_HORSE)
    ] + [{"nodeId": "S03", "resourceType": P.FAST_HORSE,
          "count": 1, "claimRound": 7}]
    warden = WardenStrategy()
    check("Warden竞速段不领取7帧负收益快马",
          warden._claim_en_route(long_horse, "S03") is None)
    long_horse.resource_config[-1]["claimRound"] = 2
    action = warden._claim_en_route(long_horse, "S03")
    check("Warden保留既有2帧顺路快马",
          action and action.get("action") == "CLAIM_RESOURCE"
          and action.get("resourceType") == P.FAST_HORSE, str(action))


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
    obstacle_eta = hybrid._gate_eta(obstacle, obstacle.me)
    guard_eta = hybrid._gate_eta(guard, guard.me)
    check("无阻挡时宫门ETA可达", open_eta < 999, f"eta={open_eta}")
    check("公开障碍按规则税保持有限可达",
          open_eta < obstacle_eta < 999,
          f"open={open_eta} obstacle={obstacle_eta}")
    check("资源足够时敌卡按一拍攻坚保持有限可达",
          open_eta < guard_eta < 999,
          f"open={open_eta} guard={guard_eta}")
    no_ammo = make_state(my_node="S09", opp_node="S02",
                         enemy_guards=("S10",))
    no_ammo.me["goodFruit"] = hybrid.GATE_GOOD_FRUIT_FLOOR
    no_ammo.me["badFruit"] = 0
    check("没有确定拆卡资源时保证型ETA才判不可达",
          hybrid._gate_eta(no_ammo, no_ammo.me) == 999)
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


def test_moving_pivot_replay_contracts():
    """04/05复盘：在途只为确定敌卡或确定拒止放弃已有进度。"""
    short = make_state(
        predicted_short_gate_start(), round_no=327,
        my_node="S10", opp_node="S09",
        my_edge=("E17", "S10", "S08"), my_edge_progress=P.BASE_SPEED,
        opp_edge=("E25", "S09", "S12"), opp_edge_progress=0)
    hybrid = HybridStrategy()
    hybrid.mode = hybrid.MODE_MOBILE
    hybrid.on_start(short)
    action = hybrid._main_action(hybrid.decide(short))
    check("04短边图不为非拒止软税清空在途进度",
          action is None, str(action))

    # 服务端执行改边后，起点仍是 S10、目标变为 S11。同一条公开证据
    # 不得让策略下一帧再次清空进度换回其它边。
    edge = short.graph.edges["E06"]
    short.me.update(
        currentNodeId="S10", nextNodeId="S11", routeEdgeId="E06",
        edgeTotalMs=short.graph.edge_total_move(edge),
        edgeProgressMs=P.BASE_SPEED, state=P.ST_MOVING)
    check("非拒止路线证据下一帧仍不触发横跳",
          hybrid._moving_pivot_action(short) is None)

    same_edge = make_state(
        predicted_split_corridors_start(), round_no=2,
        my_node="S01", opp_node="S01",
        my_edge=("E15", "S01", "S06"), my_edge_progress=P.BASE_SPEED,
        opp_edge=("E15", "S01", "S06"), opp_edge_progress=P.BASE_SPEED)
    hybrid_same = HybridStrategy()
    hybrid_same.mode = hybrid_same.MODE_MOBILE
    hybrid_same.on_start(same_edge)
    check("同边同进度不因非对称天气假设误判设卡威胁",
          not hybrid_same._moving_target_guard_threat(same_edge, "S06"))

    optional = make_state(
        predicted_optional_s02_start(), round_no=199,
        my_node="S08", opp_node="S11",
        enemy_guards=("S11",),
        my_edge=("E17", "S08", "S10"),
        my_edge_progress=18 * P.BASE_SPEED,
        opp_edge=("E06", "S11", "S10"), opp_edge_progress=0)
    hybrid2 = HybridStrategy()
    hybrid2.mode = hybrid2.MODE_MOBILE
    hybrid2.on_start(optional)
    check("05可选S02图在S10成卡前改走S09",
          hybrid2._main_action(hybrid2.decide(optional)) == P.a_move("S09"))

    near = make_state(
        predicted_optional_s02_start(), round_no=199,
        my_node="S08", opp_node="S11",
        my_edge=("E17", "S08", "S10"),
        my_edge_progress=optional.me["edgeTotalMs"] - 3 * P.BASE_SPEED,
        opp_edge=("E06", "S11", "S10"), opp_edge_progress=0)
    hybrid3 = HybridStrategy()
    check("我方将在设卡生效前到站时不误判冻结威胁",
          not hybrid3._moving_target_guard_threat(near, "S10"))


def test_replay_gate_lead_survives_obstacle_combo():
    """replay.report (1)：S02 的处理先手不能再被 S03 回头卖掉。"""
    state = make_state(
        variant1_e25_start(), round_no=54, my_node="S02", opp_node="S02",
        obstacle_nodes=("S06", "S07", "S10", "S11"))
    hybrid = HybridStrategy()
    hybrid.mode = hybrid.MODE_MOBILE
    hybrid.warden._processed_nodes.add("S02")
    hybrid.warden._processed_here = True
    hybrid.planner._processed_here = True

    eta = hybrid._gate_eta(state, state.me, optimistic=False)
    check("组合图公开障碍不再把宫门ETA打成999", eta < 999, f"eta={eta}")
    check("S02处理先手能启动宫门领先保护",
          hybrid._should_preserve_gate_lead(state), f"eta={eta}")

    task = {
        "taskId": "T_S03", "nodeId": "S03", "processRound": 3,
        "score": 30, "active": True,
    }
    plan = Plan("task", task=task, position="S03")
    budget = hybrid._gate_lead_budget(state)
    cost = hybrid._gate_plan_opportunity_cost(state, plan)
    actions = hybrid._gate_pace_actions(state, [P.a_move("S03")], plan)
    main = next((a for a in actions if a["action"] in P.MAIN_ACTION_TYPES), None)
    check("S03支线完整成本超过设卡先手预算", cost > budget,
          f"cost={cost} budget={budget}")
    check("领先保护覆盖Planner的S03回头动作",
          main and not (main["action"] == "MOVE"
                        and main.get("targetNodeId") == "S03"), str(main))

    consecutive = make_state(
        variant1_start(), round_no=360, phase=P.PHASE_RUSH,
        my_node="S10", opp_node="S02", obstacle_nodes=("S11",))
    warden = WardenStrategy()
    warden._last_forced_node = "S10"
    action = warden._advance(consecutive, "S10", "S11")
    check("连续障碍不重复强通而改用合法清障",
          action.get("action") == "CLEAR"
          and action.get("targetNodeId") == "S11", str(action))


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


def test_opponent_verify_lower_bound():
    """拒止证明必须覆盖对手探路/破关令的最短宫门验核。"""
    state = make_state(round_no=450, phase=P.PHASE_RUSH,
                       my_node="S09", opp_node="S14")
    hybrid = HybridStrategy()
    gate_term, path = hybrid.warden._travel_dynamic(
        state, state.gate_node, state.terminal_node,
        P.RUSH_SPEED, 15, include_intermediate_process=False,
        conservative_weather=False)
    expected = 2 + gate_term + 2
    actual = hybrid._opponent_finish_lower_bound(state, 0)
    check("对手交付下界按2帧最强验核而非普通6帧",
          bool(path) and actual == expected,
          f"expected={expected} actual={actual}")


def test_configurable_resources_and_held_wall_budget():
    """地图读条和粘性计划都不能偷吃设卡/交付安全垫。"""
    resource = make_state(round_no=180, my_node="S03", opp_node="S09")
    for item in resource.resource_config:
        if item.get("nodeId") == "S03" \
                and item.get("resourceType") == P.ICE_BOX:
            item["claimRound"] = 7
    hybrid = HybridStrategy()
    plan = {"target": "S09", "myEta": 0, "oppEta": 10}
    action = P.a_claim_resource("S03", P.ICE_BOX)
    check("可变资源领取帧从地图读取而非固定2帧",
          hybrid._resource_claim_frames(resource, "S03", P.ICE_BOX) == 7
          and hybrid.planner.planner._resource_claim_frames(
              resource, "S03", P.ICE_BOX) == 7
          and hybrid.warden._resource_claim_frames(
              resource, "S03", P.ICE_BOX) == 7
          and not hybrid._action_fits_mobile_lead(resource, action, plan))

    held_state = make_state(round_no=360, phase=P.PHASE_NORMAL,
                            my_node="S05", opp_node="S03")
    held = HybridStrategy()
    held.mobile_target = "S09"
    held.mobile_plan = {"target": "S09", "denial": False}
    check("粘性动态墙不足完整5帧设卡余量时自动解除",
          held._held_mobile_plan(held_state) is None)


def test_opponent_future_horse_stock_lower_bound():
    """尚未领取的公开马也必须进入对手最快到门下界。"""
    without = make_state(round_no=120, my_node="S09", opp_node="S01")
    with_stock = make_state(round_no=120, my_node="S09", opp_node="S01")
    for state in (without, with_stock):
        for node_id in state.nodes:
            state.nodes[node_id]["resourceStock"] = {}
    with_stock.nodes["S03"]["resourceStock"] = {P.FAST_HORSE: 2}
    hybrid = HybridStrategy()
    slow = hybrid._opponent_legal_gate_eta(without, without.opp)
    fast = hybrid._opponent_legal_gate_eta(with_stock, with_stock.opp)
    check("拒止证明把沿图未领取快马让给对手", fast < slow,
          f"withStock={fast} without={slow}")


def test_guard_escape_and_gate_wall_lower_bounds():
    """堵人证明覆盖改边回攻、破关令与最快强通。"""
    shortcut = public_v42_start()
    for edge in shortcut["edges"]:
        if edge.get("edgeId") in ("E17", "E22"):
            edge["distance"] = 1
    state = make_state(
        shortcut, round_no=200, my_node="S10", opp_node="S09",
        opp_edge=("E05", "S09", "S10"), opp_edge_progress=0)
    warden = WardenStrategy()
    edge_remain = warden._opp_edge_remaining(state)
    reentry = warden._mobile_reentry_escape_delay(
        state, "S10", edge_remain)
    delay = warden._mobile_guard_delay(state, "S10", 0, edge_remain)
    check("短三角允许改边回攻时不误证动态墙收益",
          reentry == 0 and delay == 0,
          f"edgeRemain={edge_remain} reentry={reentry} delay={delay}")

    breakable = make_state(round_no=450, phase=P.PHASE_RUSH,
                           my_node="S14", opp_node="S13")
    breakable.opp.update(goodFruit=2, badFruit=0,
                         squadAvailable=0, rushTacticUsedCount=0)
    check("对手可用破关令一拍拆防4时宫门墙下界为0",
          warden._gate_wall_hold_lower_bound(breakable) == 0)

    forced = make_state(round_no=450, phase=P.PHASE_RUSH,
                        my_node="S14", opp_node="S13")
    forced.opp.update(goodFruit=0, badFruit=0,
                      squadAvailable=0, rushTacticUsedCount=1)
    check("对手不能攻坚时宫门墙仍按最快32帧强通而非120帧风化",
          warden._gate_wall_hold_lower_bound(forced) == 32)


def main():
    test_route_and_weather_formula()
    test_processing_rules()
    test_current_edge_and_public_blocks()
    test_task_budget_and_gate_topology()
    test_mobile_intercept_uses_conservative_eta()
    test_moving_pivot_replay_contracts()
    test_replay_gate_lead_survives_obstacle_combo()
    test_finish_buffer_boundary()
    test_opponent_verify_lower_bound()
    test_configurable_resources_and_held_wall_budget()
    test_opponent_future_horse_stock_lower_bound()
    test_guard_escape_and_gate_wall_lower_bounds()
    print("ALL GATE PROOF SCENARIOS PASS")


if __name__ == "__main__":
    main()

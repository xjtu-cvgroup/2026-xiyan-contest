#!/usr/bin/env python3
"""组委会 Tips 驱动的隐藏地图预测与快速对局矩阵。"""
import argparse
import copy
import json
import os
import sys

from arena import Arena, PID_A, PID_B
from lychee import protocol as P
from lychee.hybrid import HybridStrategy
from lychee.state import GameState
from scenario_maps import (
    predicted_optional_s02_start,
    predicted_resource_shuffle_start,
    predicted_s02_fast_horse_start,
    predicted_short_gate_start,
    predicted_single_s10_bypass_start,
    predicted_split_corridors_start,
)
from sparring import RoadFarmerBot, RusherBot


class S02ResourceRusher(RusherBot):
    """按组委会提示：主动去首窗站抢资源，再走当局最快路线冲宫门。"""

    def bot_action(self, state):
        me = state.me
        if me.get("routeEdgeId"):
            return super().bot_action(state)
        cur = me.get("currentNodeId")
        if cur == state.start_node:
            return self._walk_to(state, "S02")
        if cur == "S02":
            claim = self._claim_here(
                state, (P.FAST_HORSE, P.SHORT_HORSE, P.ICE_BOX,
                        P.PASS_TOKEN, P.OFFICIAL_PERMIT))
            if claim:
                return claim
            return self._walk_to(state, state.gate_node) or P.a_wait()
        return super().bot_action(state)


VARIANTS = (
    {
        "id": "01-s02-fast-horse",
        "factory": predicted_s02_fast_horse_start,
        "summary": "S02投放快马，隔离测试先抢资源与先处理的时序博弈",
        "primary": "S10", "reaction": True,
    },
    {
        "id": "02-single-s10-bypass",
        "factory": predicted_single_s10_bypass_start,
        "summary": "S02快马 + S09直达S11，单旁路使S10不再必经",
        "primary": None, "reaction": True,
    },
    {
        "id": "03-split-corridors",
        "factory": predicted_split_corridors_start,
        "summary": "官水线与山线各自绕S10，只有S14稳定汇合",
        "primary": None, "reaction": True,
    },
    {
        "id": "04-short-gate-edges",
        "factory": predicted_short_gate_start,
        "summary": "S14仍必经，但全部入边短于反应设卡生效窗",
        "primary": None, "reaction": False,
    },
    {
        "id": "05-optional-s02",
        "factory": predicted_optional_s02_start,
        "summary": "S02有冰鉴，但无障碍山线明显更快，窗口可争而非必争",
        "primary": None, "reaction": True,
    },
    {
        "id": "06-resource-shuffle",
        "factory": predicted_resource_shuffle_start,
        "summary": "双旁路 + 马匹重排，测试资源变化下的领先预算",
        "primary": None, "reaction": True,
    },
)


def _state(start):
    state = GameState(PID_A)
    state.on_start(json.loads(json.dumps(start)))
    return state


def _topology_row(spec):
    start = spec["factory"]()
    state = _state(start)
    hybrid = HybridStrategy()
    primary = hybrid._mandatory_primary_choke(state)
    s10_mandatory = not hybrid._reachable_without(
        state, state.start_node, state.gate_node, "S10")
    s14_mandatory = not hybrid._reachable_without(
        state, state.start_node, state.terminal_node, state.gate_node)
    reaction = hybrid._gate_has_reaction_window(state)
    s02_resources = [
        r["resourceType"] for r in state.resource_config
        if r.get("nodeId") == "S02"
    ]
    _, fastest = state.graph.shortest_path(state.start_node, state.gate_node)
    ok = (s14_mandatory and primary == spec["primary"]
          and reaction == spec["reaction"])
    return {
        "id": spec["id"], "ok": ok, "primary": primary,
        "s10Mandatory": s10_mandatory, "s14Mandatory": s14_mandatory,
        "reaction": reaction, "s02Resources": s02_resources,
        "fastest": fastest,
    }


def _first_s02_main(timeline, pid):
    for frame in timeline:
        player = frame["players"][pid]
        if player["node"] == "S02" and not player["edge"]:
            main = frame["actions"][pid]["main"]
            if main:
                return frame["round"], main.get("action"), \
                    main.get("resourceType")
    return None


def _first_guard(timeline, pid, team):
    seen = set()
    for frame in timeline:
        for node_id, guard in frame["guards"].items():
            if node_id in seen or guard.get("team") != team:
                continue
            seen.add(node_id)
            return frame["round"], node_id
    return None


def _run_match(spec, opponent, seat, seed):
    if seat == "A":
        cls_a, cls_b, us, them, team = HybridStrategy, opponent, PID_A, PID_B, "RED"
    else:
        cls_a, cls_b, us, them, team = opponent, HybridStrategy, PID_B, PID_A, "BLUE"
    arena = Arena(
        seed, cls_a=cls_a, cls_b=cls_b, start_data=spec["factory"](),
        obstacle_nodes=(), weather_plan=[], capture_timeline=True)
    result = arena.run()
    ours, theirs = result[us], result[them]
    if ours["score"] > theirs["score"]:
        outcome = "WIN"
    elif ours["score"] < theirs["score"]:
        outcome = "LOSS"
    else:
        outcome = "DRAW"
    return {
        "variant": spec["id"], "opponent": opponent.__name__, "seat": seat,
        "outcome": outcome, "score": (ours["score"], theirs["score"]),
        "delivery": (ours["deliverRound"], theirs["deliverRound"]),
        "task": (ours["taskBase"], theirs["taskBase"]),
        "mode": arena.strategies[us].mode,
        "s02": _first_s02_main(result["timeline"], us),
        "guard": _first_guard(result["timeline"], us, team),
        "illegal": ours["illegal"],
        "errors": result["strategyErrors"],
    }


def export_maps(directory):
    os.makedirs(directory, exist_ok=True)
    for spec in VARIANTS:
        path = os.path.join(directory, spec["id"] + ".start.json")
        payload = {
            "scenario": {
                "id": spec["id"], "summary": spec["summary"],
                "source": "map_config_variant_a.json + organizer tips",
            },
            "msg_type": "start",
            "msg_data": spec["factory"](),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")


def _raw_edge(edge, source_edges):
    edge_id = edge["edgeId"]
    result = copy.deepcopy(source_edges.get(edge_id, {}))
    result.update({
        "edgeId": edge_id,
        "fromNodeId": edge.get("fromNodeId", edge.get("fromNode")),
        "toNodeId": edge.get("toNodeId", edge.get("toNode")),
        "routeType": edge["routeType"],
        "distance": edge["distance"],
        "bidirectional": edge.get("bidirectional", True),
        "pathId": edge.get("pathId", f"P_{edge_id}"),
    })
    result.pop("fromNode", None)
    result.pop("toNode", None)
    return result


def _straight_route_path(edge, nodes):
    source = nodes[edge["fromNodeId"]]
    target = nodes[edge["toNodeId"]]
    sx, sy = source["x"], source["y"]
    tx, ty = target["x"], target["y"]
    return {
        "pathId": edge["pathId"],
        "edgeId": edge["edgeId"],
        "points": [
            {"x": sx, "y": sy},
            {"x": round((sx + tx) / 2, 1),
             "y": round((sy + ty) / 2, 1)},
            {"x": tx, "y": ty},
        ],
    }


def export_raw_maps(source_path, directory):
    """导出保留 Unity 渲染字段的组委会原始 map_config 格式。"""
    with open(source_path, encoding="utf-8") as f:
        source = json.load(f)
    source_edges = {edge["edgeId"]: edge for edge in source["edges"]}
    source_paths = {
        path["edgeId"]: path for path in source.get("routePaths", [])
    }
    nodes = {node["nodeId"]: node for node in source["nodes"]}

    os.makedirs(directory, exist_ok=True)
    for index, spec in enumerate(VARIANTS, 1):
        start = spec["factory"]()
        result = copy.deepcopy(source)
        result["mapId"] = f"predicted_{spec['id'].replace('-', '_')}"
        result["mapName"] = f"预测测试：{spec['summary']}"
        result["designVersion"] = f"V4.2-PREDICT-{index:02d}"
        result["edges"] = [
            _raw_edge(edge, source_edges) for edge in start["edges"]
        ]
        result["gameplay"] = copy.deepcopy(start["map"]["gameplay"])
        result["routePaths"] = []
        for edge in result["edges"]:
            path = copy.deepcopy(source_paths.get(edge["edgeId"]))
            if path is None:
                path = _straight_route_path(edge, nodes)
            path["pathId"] = edge["pathId"]
            path["edgeId"] = edge["edgeId"]
            result["routePaths"].append(path)

        path = os.path.join(directory, spec["id"] + ".map_config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
            f.write("\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=20260714)
    parser.add_argument("--export-dir")
    parser.add_argument("--raw-map-source")
    parser.add_argument("--raw-export-dir")
    parser.add_argument("--variant", action="append",
                        choices=[spec["id"] for spec in VARIANTS])
    parser.add_argument("--opponent",
                        choices=("all", "rusher", "tip-rusher", "farmer"),
                        default="all")
    parser.add_argument("--seat", choices=("all", "A", "B"), default="all")
    parser.add_argument("--topology-only", action="store_true")
    args = parser.parse_args()
    if bool(args.raw_map_source) != bool(args.raw_export_dir):
        parser.error("--raw-map-source 与 --raw-export-dir 必须同时提供")
    if args.export_dir:
        export_maps(args.export_dir)
    if args.raw_map_source:
        export_raw_maps(args.raw_map_source, args.raw_export_dir)

    selected = [spec for spec in VARIANTS
                if not args.variant or spec["id"] in args.variant]
    topology = [_topology_row(spec) for spec in selected]
    print("TOPOLOGY")
    for row in topology:
        mark = "PASS" if row["ok"] else "FAIL"
        print(
            f"[{mark}] {row['id']:25} primary={row['primary']} "
            f"S10={row['s10Mandatory']} S14={row['s14Mandatory']} "
            f"reaction={row['reaction']} S02={row['s02Resources']} "
            f"path={'->'.join(row['fastest'])}")
    structure_ok = all(row["ok"] for row in topology)
    if args.topology_only:
        return 0 if structure_ok else 1

    matches = []
    opponents = ((RusherBot, S02ResourceRusher, RoadFarmerBot)
                 if args.opponent == "all"
                 else (RusherBot,) if args.opponent == "rusher"
                 else (S02ResourceRusher,) if args.opponent == "tip-rusher"
                 else (RoadFarmerBot,))
    seats = ("A", "B") if args.seat == "all" else (args.seat,)
    for index, spec in enumerate(selected):
        for opponent in opponents:
            for seat in seats:
                matches.append(_run_match(
                    spec, opponent, seat, args.seed + index))
    print("\nFULL MATCHES")
    for row in matches:
        print(
            f"[{row['outcome']:4}] {row['variant']:25} "
            f"vs={row['opponent']:<13} seat={row['seat']} "
            f"score={row['score'][0]}:{row['score'][1]} "
            f"delivery={row['delivery'][0]}:{row['delivery'][1]} "
            f"task={row['task'][0]}:{row['task'][1]} mode={row['mode']} "
            f"S02={row['s02']} guard={row['guard']}")

    runtime_ok = all(not row["illegal"] and not row["errors"] for row in matches)
    wins = sum(row["outcome"] == "WIN" for row in matches)
    losses = sum(row["outcome"] == "LOSS" for row in matches)
    print(
        f"\nSUMMARY topology={sum(r['ok'] for r in topology)}/{len(topology)} "
        f"runtime={'PASS' if runtime_ok else 'FAIL'} "
        f"wins={wins} draws={len(matches) - wins - losses} losses={losses}")
    return 0 if structure_ok and runtime_ok else 1


if __name__ == "__main__":
    sys.exit(main())

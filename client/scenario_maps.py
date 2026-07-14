"""离线裁判使用的真实地图变体。

基础消息来自仓库的 start消息.json；这里仅保存主办方地图文件相对该消息
发生变化的拓扑和固定处理站数据，避免把巨大的渲染图层复制进测试夹具。
"""
import json
import os


DOC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


PUBLIC_V42_EDGES = (
    {"edgeId": "E23", "fromNodeId": "S11", "toNodeId": "S14",
     "routeType": "BRANCH", "distance": 15, "bidirectional": True},
    {"edgeId": "E24", "fromNodeId": "S10", "toNodeId": "S13",
     "routeType": "BRANCH", "distance": 27, "bidirectional": True},
)

VARIANT1_DISTANCE = {
    "E01": 31, "E02": 24, "E03": 55, "E04": 48, "E05": 40,
    "E06": 36, "E07": 20, "E08": 25, "E09": 18, "E10": 10,
    "E11": 21, "E12": 45, "E13": 46, "E15": 46, "E16": 58,
    "E17": 54, "E18": 80, "E19": 50, "E20": 42, "E21": 57,
    "E22": 76,
}

VARIANT1_RESOURCES = (
    ("S03", "ICE_BOX"), ("S07", "PASS_TOKEN"), ("S03", "INTEL"),
    ("S05", "SHORT_HORSE"), ("S04", "BOAT_RIGHT"), ("S04", "INTEL"),
    ("S08", "ICE_BOX"), ("S06", "INTEL"), ("S08", "SHORT_HORSE"),
    ("S08", "PASS_TOKEN"), ("S08", "INTEL"), ("S07", "ICE_BOX"),
    ("S07", "SHORT_HORSE"), ("S11", "INTEL"), ("S09", "FAST_HORSE"),
    ("S09", "OFFICIAL_PERMIT"), ("S10", "INTEL"),
    ("S13", "PASS_TOKEN"), ("S13", "OFFICIAL_PERMIT"),
    ("S13", "INTEL"), ("S06", "OFFICIAL_PERMIT"),
    ("S06", "FAST_HORSE"),
)

VARIANT1_TASK_CANDIDATES = {
    "T01": ["S03", "S08"],
    "T08": ["S04", "S05"],
    "T04": ["S06", "S07", "S10", "S11"],
    "T02": ["S07", "S10", "S11"],
    "T06": ["S09", "S04", "S06"],
    "T11": ["S08", "S10", "S11"],
    "T12": ["S11", "S13"],
    "T13": ["S09", "S12", "S13"],
    "T14": ["S10", "S11", "S12"],
}

VARIANT1_ROUTE_BUCKETS = {
    "ROAD": ["S03", "S09", "S10", "S11", "S13"],
    "WATER": ["S04", "S05", "S09", "S10", "S12", "S13"],
    "MOUNTAIN": ["S06", "S08", "S10", "S11", "S12"],
}


def _base_start():
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        return json.load(f)["msg_data"]


def _sync_map(start):
    """平台同时在顶层和 map 内携带地图；测试两处必须保持一致。"""
    start["map"]["nodes"] = json.loads(json.dumps(start["nodes"]))
    start["map"]["edges"] = json.loads(json.dumps(start["edges"]))
    return start


def public_v42_start():
    """用户提供的公开地图：相对旧 start 消息新增 E23/E24。"""
    start = _base_start()
    known = {edge["edgeId"] for edge in start["edges"]}
    for edge in PUBLIC_V42_EDGES:
        if edge["edgeId"] not in known:
            start["edges"].append(dict(edge))
    return _sync_map(start)


def variant1_start():
    """用户提供的变种地图1：边长、E18 起点和 S04/S05 处理语义变化。"""
    start = public_v42_start()
    for edge in start["edges"]:
        eid = edge["edgeId"]
        if eid in VARIANT1_DISTANCE:
            edge["distance"] = VARIANT1_DISTANCE[eid]
        if eid == "E18":
            edge["fromNodeId"] = "S02"
            edge["toNodeId"] = "S06"
    gameplay = start["map"]["gameplay"]
    for proc in gameplay.get("processNodes") or []:
        if proc["nodeId"] == "S04":
            proc.update(processType="WATER_TRANSFER", processRound=6)
        elif proc["nodeId"] == "S05":
            proc.update(processType="BOARD", processRound=7)
    gameplay["obstacleCandidateNodeIds"] = ["S06", "S07", "S10", "S11"]
    gameplay["taskCandidates"] = json.loads(
        json.dumps(VARIANT1_TASK_CANDIDATES))
    gameplay["routeTaskBuckets"] = json.loads(
        json.dumps(VARIANT1_ROUTE_BUCKETS))
    gameplay["resources"] = [
        {"nodeId": node_id, "resourceType": resource_type,
         "count": 1, "claimRound": 2}
        for node_id, resource_type in VARIANT1_RESOURCES
    ]
    return _sync_map(start)


def e25_bypass_start(distance=85):
    """replay.report.txt 的自定义变种：S09 可经长边 E25 绕开 S10。"""
    start = public_v42_start()
    start["edges"].append({
        "edgeId": "E25", "fromNodeId": "S09", "toNodeId": "S11",
        "routeType": "BRANCH", "distance": distance,
        "bidirectional": True,
    })
    return _sync_map(start)


def variant1_e25_start(distance=85):
    """本次平台组合：变种1边长/处理站，同时存在绕 S10 的 E25。"""
    start = variant1_start()
    start["edges"].append({
        "edgeId": "E25", "fromNodeId": "S09", "toNodeId": "S11",
        "routeType": "BRANCH", "distance": distance,
        "bidirectional": True,
    })
    return _sync_map(start)


def gate_bypass_start(distance=20):
    """压力场景：终点可绕开 S14，验证策略不会机械押最终墙。"""
    start = public_v42_start()
    start["edges"].append({
        "edgeId": "E_GATE_BYPASS", "fromNodeId": "S13", "toNodeId": "S15",
        "routeType": "BRANCH", "distance": distance,
        "bidirectional": True,
    })
    return _sync_map(start)

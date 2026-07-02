"""地图图结构与寻路。

以 start/inquire 下发的 edges[] 为唯一通行依据（禁止硬编码地图，任务书 2.2）。
帧数估算按任务书 2.3.2：
  到站所需移动量 = ceil(路线距离 × 路线耗时系数)
  每帧移动量     = floor(基础每帧移动量 × 1000 / 天气通行倍率)
"""
import heapq
import math

from . import protocol as P


class MapGraph:
    def __init__(self, edges):
        self.edges = {}          # edgeId -> edge dict
        self.adj = {}            # nodeId -> [(neighborId, edge), ...]
        for e in edges:
            f = e.get("fromNodeId") or e.get("fromNode")
            t = e.get("toNodeId") or e.get("toNode")
            if not f or not t:
                continue
            self.edges[e["edgeId"]] = e
            self.adj.setdefault(f, []).append((t, e))
            if e.get("bidirectional", True):
                self.adj.setdefault(t, []).append((f, e))

    def neighbors(self, node_id):
        """合法相邻节点列表 [(nodeId, edge)]（含单向方向判断）。"""
        return self.adj.get(node_id, [])

    def edge_between(self, a, b):
        """a -> b 方向可走的路线边；没有则返回 None。"""
        for n, e in self.adj.get(a, []):
            if n == b:
                return e
        return None

    @staticmethod
    def edge_total_move(edge):
        """到站所需移动量（= edgeTotalMs）。"""
        coeff = P.ROUTE_COEFF.get(edge.get("routeType"), P.ROUTE_COEFF[P.ROAD])
        return math.ceil(edge["distance"] * coeff)

    @staticmethod
    def edge_frames(edge, per_frame=P.BASE_SPEED):
        """无天气影响下走完该边需要的结算帧数。"""
        return math.ceil(MapGraph.edge_total_move(edge) / max(1, per_frame))

    def shortest_path(self, src, dst, per_frame=P.BASE_SPEED, node_penalty=None):
        """Dijkstra 最短路（按帧数）。

        node_penalty(nodeId) -> 附加帧数成本，用于绕开障碍 / 敌方设卡；
        返回 (总帧数, [src, ..., dst])；不可达返回 (inf, [])。
        """
        if src == dst:
            return 0, [src]
        dist = {src: 0}
        prev = {}
        pq = [(0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if u == dst:
                break
            if d > dist.get(u, math.inf):
                continue
            for v, e in self.adj.get(u, []):
                cost = self.edge_frames(e, per_frame)
                if node_penalty and v != dst:
                    cost += node_penalty(v)
                nd = d + cost
                if nd < dist.get(v, math.inf):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
        if dst not in dist:
            return math.inf, []
        path = [dst]
        while path[-1] != src:
            path.append(prev[path[-1]])
        path.reverse()
        return dist[dst], path

    def shortest_distance(self, src, dst):
        """按路线距离（不是帧数）的最短累计距离，用于宫宴冲刺触发判断等口径。"""
        dist = {src: 0}
        pq = [(0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if u == dst:
                return d
            if d > dist.get(u, math.inf):
                continue
            for v, e in self.adj.get(u, []):
                nd = d + e["distance"]
                if nd < dist.get(v, math.inf):
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v))
        return math.inf

    def next_hop(self, src, dst, per_frame=P.BASE_SPEED, node_penalty=None):
        """去 dst 的下一站；不可达返回 None。"""
        _, path = self.shortest_path(src, dst, per_frame, node_penalty)
        return path[1] if len(path) >= 2 else None

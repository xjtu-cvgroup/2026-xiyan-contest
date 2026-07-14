"""隐藏地图混合策略：传统得分底盘 + 必经关隘/S14 守望者。

战略只做互斥切换，不把三套估值揉在同一帧里：
- 当前地图存在真正必经的 KEY_PASS：完整沿用 Warden；
- KEY_PASS 可绕：Planner 最大化得分；
- Planner 局后段能严格证明抢到宫门先手：粘性切入 S14 Warden。
"""
import math

from . import protocol as P
from .planner import RUSH_EARLIEST
from .strategy import PlannerStrategy, Strategy
from .warden import DELIVER_FRAMES, RUSH_SPEED_FRAMES, WardenStrategy


class HybridStrategy(Strategy):
    MODE_PRIMARY = "PRIMARY_WARDEN"
    MODE_SCORE = "SCORE_RACE"
    MODE_GATE = "GATE_WARDEN"

    # S14 设卡 T->T+4 完成、T+5 才拦。再留 3 帧 ETA 误差，只有明确先手
    # 才放弃传统策略的后续任务机会。
    GATE_LEAD_MARGIN = 8
    GATE_TASK_FLOOR = 120
    GATE_COMMIT_ROUND = RUSH_EARLIEST - 40
    GATE_MAX_ETA = 150
    GATE_THREAT_ETA = 120
    GATE_GOOD_FRUIT_FLOOR = 7  # 防4宫门卡 2 篓 + Warden 5 篓底仓

    def __init__(self, logger=None):
        self.log = logger
        self.planner = PlannerStrategy(logger)
        self.warden = WardenStrategy(logger)
        self.mode = None
        self.primary_choke = None

    def on_start(self, state):
        self.planner.on_start(state)
        self.warden.on_start(state)

    def decide(self, state):
        if self.mode is None:
            self.primary_choke = self._mandatory_primary_choke(state)
            if self.primary_choke:
                self.warden.force_camp(self.primary_choke)
                self.mode = self.MODE_PRIMARY
            else:
                self.mode = self.MODE_SCORE
            if self.log:
                self.log.info("hybrid: initial mode=%s primary=%s",
                              self.mode, self.primary_choke)

        if self.mode == self.MODE_SCORE and self._should_commit_gate(state):
            self._activate_gate_control(state)

        if self.mode in (self.MODE_PRIMARY, self.MODE_GATE):
            return self.warden.decide(state)
        intercept = self._score_mobile_intercept(state)
        if intercept:
            return [intercept]
        return self.planner.decide(state)

    # ================= 地图资格审查 =================

    @staticmethod
    def _reachable_without(state, src, dst, blocked):
        if not src or not dst or src == blocked or dst == blocked:
            return False
        seen = {src}
        stack = [src]
        while stack:
            cur = stack.pop()
            if cur == dst:
                return True
            for nxt, _ in state.graph.neighbors(cur):
                if nxt == blocked or nxt in seen:
                    continue
                seen.add(nxt)
                stack.append(nxt)
        return False

    def _mandatory_primary_choke(self, state):
        """返回所有起点到宫门路径都必经的第一个 KEY_PASS。"""
        if not state.graph:
            return None
        start, gate = state.start_node, state.gate_node
        _, path = state.graph.shortest_path(start, gate)
        if not path:
            return None
        for node_id in path[1:-1]:
            if state.node(node_id).get("nodeType") != "KEY_PASS":
                continue
            if not self._reachable_without(state, start, gate, node_id):
                return node_id
        return None

    # ================= Planner -> S14 粘性接管 =================

    @staticmethod
    def _edge_remaining_frames(player, optimistic=False):
        total = player.get("edgeTotalMs") or 0
        done = player.get("edgeProgressMs") or 0
        remain = max(0, total - done)
        speed = P.SPEED_RUSH if optimistic else P.BASE_SPEED
        return int(math.ceil(remain / max(1, speed)))

    def _gate_eta(self, state, player, optimistic=False):
        if not player or player.get("delivered") or player.get("retired"):
            return 999
        moving = bool(player.get("routeEdgeId") and player.get("nextNodeId"))
        anchor = player.get("nextNodeId") if moving \
            else player.get("currentNodeId")
        if not anchor:
            return 999

        edge_frames = self._edge_remaining_frames(player, optimistic) \
            if moving else 0
        if optimistic:
            boost_type, boost_rem = P.RUSH_SPEED, RUSH_SPEED_FRAMES
        else:
            boost_type, boost_rem, _ = self.warden._active_speed_buff(
                state, player)
        boost_type, boost_rem = self.warden._consume_boost(
            boost_type, boost_rem, edge_frames)

        include_current = False
        if moving:
            include_current = anchor != state.gate_node
        elif player.get("playerId") == state.player_id:
            node = state.node(anchor)
            include_current = bool(
                node.get("processType")
                and node.get("processType") != "VERIFY"
                and not self.planner._processed_here)

        eta, path = self.warden._travel_dynamic(
            state, anchor, state.gate_node, boost_type, boost_rem,
            start_elapsed=edge_frames,
            include_current_process=include_current,
            include_intermediate_process=not optimistic,
            conservative_weather=not optimistic)
        return eta if path else 999

    def _my_finish_need(self, state, gate_eta):
        gate_term, path = self.warden._travel_dynamic(
            state, state.gate_node, state.terminal_node,
            conservative_weather=True)
        if not path:
            return 999
        rush_wait = 0
        if state.phase != P.PHASE_RUSH:
            rush_wait = max(0, RUSH_EARLIEST - (state.round + gate_eta))
        return gate_eta + rush_wait + self.warden._gate_verify_frames(state) \
            + gate_term + DELIVER_FRAMES

    def _score_mobile_intercept(self, state):
        """旁路图保留 Planner，只叠加 2621 式已承诺路线截击。"""
        me = state.me
        if not me or me.get("verified") or me.get("delivered") \
                or me.get("retired"):
            return None
        my_eta = self._gate_eta(state, me, optimistic=False)
        finish_need = self._my_finish_need(state, my_eta)
        slack = state.duration_round - state.round \
            - finish_need - self.warden.EXIT_PAD
        return self.warden.mobile_intercept_action(state, slack)

    def _should_commit_gate(self, state):
        me, opp = state.me, state.opp
        if not me or not opp or me.get("verified"):
            return False
        if opp.get("verified") or opp.get("delivered") \
                or opp.get("retired"):
            return False
        if opp.get("currentNodeId") == state.gate_node \
                and not opp.get("routeEdgeId"):
            return False
        if (me.get("goodFruit", 0) or 0) < self.GATE_GOOD_FRUIT_FLOOR:
            return False
        if not self._gate_has_reaction_window(state):
            return False

        my_eta = self._gate_eta(state, me, optimistic=False)
        opp_eta = self._gate_eta(state, opp, optimistic=True)
        if my_eta >= 999 or my_eta > self.GATE_MAX_ETA:
            return False
        if my_eta + self.GATE_LEAD_MARGIN > opp_eta:
            return False
        strategic_ready = ((me.get("taskScore", 0) or 0)
                           >= self.GATE_TASK_FLOOR
                           or state.round >= self.GATE_COMMIT_ROUND
                           or state.phase == P.PHASE_RUSH
                           or opp_eta <= self.GATE_THREAT_ETA)
        if not strategic_ready:
            return False

        remain = state.duration_round - state.round
        finish_need = self._my_finish_need(state, my_eta)
        return finish_need + self.warden.EXIT_PAD <= remain

    def _gate_has_reaction_window(self, state):
        """S14 不可由终点反穿，且所有正常入边给足反应卡生效窗。"""
        # 任务书 2.3.1：未验核队伍可从 S15 返回 S14，且无视 S14 卡。
        # 若存在绕过宫门直达终点的路径，宫门墙从拓扑上就不成立。
        if self._reachable_without(
                state, state.start_node, state.terminal_node,
                state.gate_node):
            return False
        inbound = []
        for src in state.graph.adj:
            if src == state.terminal_node:
                continue
            for dst, edge in state.graph.neighbors(src):
                if dst == state.gate_node:
                    inbound.append(state.graph.edge_frames(
                        edge, P.SPEED_RUSH))
        return bool(inbound) and min(inbound) >= self.warden.GUARD_MIN_LEAD

    def _activate_gate_control(self, state):
        self.mode = self.MODE_GATE
        self.warden.force_camp(state.gate_node)
        # Planner 已经实际花掉的弹药同步给 Warden 的字段缺失兜底账本。
        self.warden._squad_spent = self.planner._squad_spent
        self.warden._rush_tactic_tried = self.planner._rush_tactic_tried
        self.warden._guard_sent.update(self.planner._guard_sent)
        self.warden._clear_sent.update(self.planner._clear_sent)
        cur = state.me.get("currentNodeId")
        if cur and not state.me.get("routeEdgeId") \
                and self.planner._processed_here:
            self.warden._processed_nodes.add(cur)
            if cur == "S02":
                self.warden._processed_here = True
        if self.log:
            self.log.info("hybrid: sticky gate control activated @%s", cur)

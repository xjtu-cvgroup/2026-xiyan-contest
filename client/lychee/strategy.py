"""策略层。

Strategy 是接口；BaselineStrategy 是能完整跑通「赶路 → 站点处理 → 验核 → 交付」
主线的基线实现，作为后续策略迭代的骨架：
- 每帧返回 actions[]（最多 1 主车队动作 + 1 小分队动作 + 1 窗口出牌 + 1 急策）；
- 用 events[]/actionResults[] 反馈修正本地状态（如站点处理是否完成）。
"""
from . import protocol as P


class Strategy:
    def on_start(self, state):
        """收到 start 后调用一次。"""

    def decide(self, state):
        """每帧调用，返回 actions 列表（可为空）。"""
        return []


class BaselineStrategy(Strategy):
    # 绕行惩罚：被阻挡节点在寻路中的附加帧数（可绕就绕，绕不开仍会走过去再处理）
    BLOCK_PENALTY = 120
    # 鲜度低于该值且手里有冰鉴时使用（阈值 70 之下每 10 点掉 1 篓好果）
    USE_ICE_BELOW = 72

    def __init__(self, logger=None):
        self.log = logger
        self._last_stationary_node = None   # 上一次停靠的节点
        self._processed_here = False        # 当前停靠节点的固定处理是否已完成/无需处理

    # ================= 主入口 =================

    def decide(self, state):
        self._absorb_feedback(state)
        actions = []

        # 1) 窗口出牌（每帧最多 1 张，多窗口时其余自动弃权）
        contests = state.my_open_contests()
        if contests:
            c = contests[0]
            actions.append(P.a_window_card(c["contestId"], self.pick_card(state, c)))

        # 2) 主车队动作
        main = self.main_action(state)
        if main:
            actions.append(main)

        return actions

    # ================= 反馈吸收 =================

    def _absorb_feedback(self, state):
        me = state.me
        cur = me.get("currentNodeId")
        stationary = not me.get("routeEdgeId")

        # 到达新节点：该站处理状态重置
        if stationary and cur != self._last_stationary_node:
            self._last_stationary_node = cur
            self._processed_here = False

        for action, code in state.my_rejections():
            if code == P.E_PROCESS_REQUIRED:        # 想走但没处理完 -> 先处理
                self._processed_here = False
            elif code == P.E_PROCESS_NOT_AVAILABLE:  # 此站无处理流程
                self._processed_here = True
            if self.log:
                self.log.info("r%d rejected: %s %s", state.round, action, code)

        for e in state.my_events("PROCESS_COMPLETE", "VERIFY_GATE_COMPLETE"):
            self._processed_here = True

    # ================= 主车队决策 =================

    def main_action(self, state):
        me = state.me
        if not me or me.get("retired"):
            return None

        st = me.get("state")
        # 处理/验核/休整/窗口/强制通行中：提交动作不生效，等就行
        if st in P.BUSY_STATES:
            return None
        if me.get("delivered"):
            return None  # 交付后除 WAIT 外都算违规，空动作最安全

        cur = me.get("currentNodeId")
        moving = bool(me.get("routeEdgeId"))
        if moving:
            return None  # 系统等待会继续前进；改道策略留给后续版本

        gate, terminal = state.gate_node, state.terminal_node
        verified = me.get("verified")

        # --- 终点交付 ---
        if cur == terminal:
            if verified and me.get("goodFruit", 0) > 0 and me.get("freshness", 0) > 0:
                return P.a_deliver()
            return P.a_wait()

        # --- 宫门验核（只在 RUSH 阶段开放） ---
        if cur == gate and not verified:
            if state.phase == P.PHASE_RUSH:
                return P.a_verify_gate()
            return P.a_wait()  # 宫宴冲刺未触发，等待

        # --- 固定处理站点：先完成处理才能离站 ---
        node = state.node(cur)
        needs_process = (node.get("processType") and node.get("processType") != "VERIFY"
                         and node.get("processRound", 0) > 0)
        if needs_process and not self._processed_here:
            return P.a_process()

        # --- 机会性保鲜 ---
        res = me.get("resources") or {}
        if me.get("freshness", 100) < self.USE_ICE_BELOW and res.get(P.ICE_BOX, 0) > 0:
            return P.a_use_resource(P.ICE_BOX)
        stock = node.get("resourceStock") or {}
        if stock.get(P.ICE_BOX, 0) > 0 and res.get(P.ICE_BOX, 0) == 0:
            return P.a_claim_resource(cur, P.ICE_BOX)

        # --- 赶路：未验核去宫门，已验核去终点 ---
        target = terminal if verified else gate
        nxt = self._route_next_hop(state, cur, target)
        if nxt is None:
            return P.a_wait()

        # 下一站被挡且绕不开：先尝试清障（好果够时），否则等待
        if state.has_obstacle(nxt):
            if me.get("goodFruit", 0) > 1:
                return P.a_clear(nxt)
            return P.a_wait()
        if state.enemy_guard(nxt):
            return P.a_wait()  # 攻坚/强制通行留给后续版本

        return P.a_move(nxt)

    def _route_next_hop(self, state, cur, target):
        def penalty(nid):
            return self.BLOCK_PENALTY if state.is_blocked(nid) else 0

        return state.graph.next_hop(cur, target, state.my_speed(), penalty)

    # ================= 窗口出牌 =================

    def pick_card(self, state, contest):
        """基线：有免费强行就打，否则弃权。子类可覆盖做克制博弈。"""
        if state.has_move_buff():
            return P.CARD_QIANG_XING  # 有马类/疾行令增益时强行免消耗
        return P.CARD_ABSTAIN

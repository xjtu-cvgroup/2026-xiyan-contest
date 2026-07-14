"""动态守望者策略——关键墙 + 2621 式滚动截击。

对 2026-07-05 三局实战语料（路人女主队 80:677 完美 camp、BasicPy 568:30
要塞 camp、主办方确认的中边改道机制）的极致复刻与强化，作为独立客户端
上真实环境，供主线 feat 版本对练调优。

套路（与主线 PlannerStrategy 完全不同的价值观——不比分数，比对面
交付不了）：

1. 竞速段：最快路线直奔关键关隘（本图 S10 武关；按节点类型自动识别，
   换图不失效）。开局第 1 帧派小分队清掉沿途与关隘上的道路障碍
   （用人手买帧数，也拆掉对手"障碍强通 8 帧税免冻直达"的白菜价把手）；
   顺路只领马与文书（马=帧数，文书=通行窗口防守弹药），窗口牌一律
   不烧好果——好果是墙的砖。
2. 封锁段：驻守关隘，虚卡待敌——绝不预立卡（预立卡会被对手强通有界
   买穿，这是套路成立的核心纪律）。对手一踏上任何通往关隘的边，
   立即落防 6 反应卡（读条 4 帧 << 任何来边），卡亡即补，人手全部
   留给增援续防；对手对卡强通时，我们作为守方打通行窗口（兵争克
   强行，鲜供克兵争，按拍交替防猜测）。驻守期间顺手吃关隘上的
   任务刷新波（白捡的分）。
3. 收官段：两个离场触发取其早——① 数学判死：对手从当前位置全速
   （含骑马余量）到终点已不可能，立刻动身；② 死线余量：剩余帧数
   触及自身交付需求 + 余量。离场时若对手仍在逼近，落一张临别卡
   （2735 的招牌）再走。宫门等 RUSH、验核、交付。

V3.98 保留主墙的验证强度，并把移动截击作为窄动作叠加；隐藏旁路图的
任务、资源、鲜度和悬赏仍由 Planner 负责，避免把整套得分底盘推倒重来。
"""
import math

from . import protocol as P
from .planner import TaskPlanner
from .strategy import BaselineStrategy

GATE_VERIFY_FRAMES = 6
DELIVER_FRAMES = 2
FAST_HORSE_FRAMES = 20
SHORT_HORSE_FRAMES = 14
RUSH_SPEED_FRAMES = 15
UNKNOWN_WEATHER_WINDOWS = ((80, 120), (200, 240),
                           (320, 360), (440, 480))
FARM_TASK_RACE_MARGIN = 2  # S02转农任务必须真实完成领先，不追同帧/贴身局
FARM_TASK_CAP = 150        # 未交付刷分以任务基础分封顶为一阶目标
FARM_CONTEST_DISCOUNT = 0.28
FARM_REFRESH_VALUE = 8     # 无活跃任务时，蹲任务候选点的保底期望
FARM_BUCKET_VALUE_MULT = 0.32
FARM_BUCKET_HISTORY_WEIGHT = 0.55
FARM_BUCKET_VALUE_CAP = 70
FARM_SAME_BUCKET_LEAD_PENALTY = 24
FARM_RESOURCE_VALUE = {
    P.FAST_HORSE: 16,
    P.SHORT_HORSE: 11,
}


class WardenStrategy(BaselineStrategy):
    """S10 守望者：竞速占关 → 虚卡封锁 → 判死离场。"""

    GUARD_EXTRA = 2            # 反应卡额外投入（关键关隘 → 防 6）
    GUARD_RETRY_GAP = 25       # 同节点补卡最小间隔（防拒绝风暴）
    MOBILE_GUARD_RETRY_GAP = 5  # 对手重新踏边后允许快速复卡
    MOBILE_GUARD_MIN_DELAY = 6  # 至少制造 6 帧确定延误才暴露截击意图
    MOBILE_GUARD_PAD = 5       # 4 帧读条完成，下一帧开始阻塞
    FRUIT_RESERVE = 5          # 好果底仓：交付要求 >0，窗口牌还要嚼几篓
    EXIT_PAD = 10              # 终点安全余量：覆盖帧序/天气/处理站量化误差
    FARM_DEAD_PAD = 0          # 真到不了终点才转农；安全垫只用于离墙
    HANDOFF_GUARD_DEFENSE = 2  # S10 卡快风化时提前转 S14 接墙
    HANDOFF_EXIT_PAD = 10      # S10->S14 接墙只在最后可行窗口内启动
    START_ROUTE_TAX_PAD = 4    # 首跳含税 ETA 量化余量；只用于主墙竞速
    GATE_GUARD_DEFENSE = 4     # 宫门 extra=1 拉满防4；只作接墙证明下界
    GUARD_DECAY_FRAMES = 30    # 设卡每 30 帧风化 1 点防守
    OPP_SPEED_MARGIN = 0.8     # 判死时对手 ETA 打折（骑马/疾行令余量）
    OPP_DEAD_BUFFER = 8        # 判死额外缓冲：宁可多守 8 帧不误判
    WEAKEN_RESEND_GAP = 12     # 被冻在边上时的削弱重发间隔
    # 竞速段只领快马（领取 2 帧、E05 骑 20 帧×1200 净省 ~2）：短程马
    # 净收益 ~0、文书/冰各 -2 帧——竞速期每帧都是 S10 所有权，牌弹药
    # 靠初始 4 点护卫行动点（兵争）兜底
    CLAIM_EN_ROUTE = (P.FAST_HORSE,)
    THREAT_ETA = 90            # 对手离关隘 ETA 在此内算"在逼近"（临别卡用）
    # 处理站探路标记（任务书 6.4.1：处理帧 -3 最低 2，寿命 45 帧，1 人手）：
    # 水路 S02 交接 4→2 / S04 登船 7→4 / S05 换运 6→3，3 人手买 8 帧。
    # 距站 ETA ≤ 此值才派（落地延迟 3~15 帧 + 寿命 45，窗口刚好盖住到站）
    SCOUT_DISPATCH_ETA = 38
    SQUAD_WEAKEN_COST = 2
    SQUAD_CLEAR_COST = 2
    SQUAD_REINFORCE_COST = 2
    SQUAD_SCOUT_COST = 1
    SQUAD_NONURGENT_RESERVE = 4  # 非转农期至少留两次削弱/续防弹药

    def __init__(self, logger=None, forced_camp=None):
        super().__init__(logger)
        self.planner = TaskPlanner(logger)
        self._forced_camp = forced_camp
        self.camp_node = None
        self._plans_ready = False
        self._clear_plan = []      # 待派小分队清障的节点（按途经顺序）
        self._scout_plan = []      # 待标记的固定处理站（按途经顺序）
        self._scout_sent = {}      # nodeId -> 派出帧
        self._clear_sent = {}      # nodeId -> 派出帧
        self._guard_sent = {}      # nodeId -> 提交帧
        self._reinforce_sent = -999
        self._weaken_sent = {}     # nodeId -> 派出帧（被冻自救）
        self._squad_spent = 0
        self._dead_since = None    # 双死判定首次成立的帧（懦夫博弈滞后）
        self._processed_here = False   # 兼容旧 S02 状态；新逻辑用 _processed_nodes
        self._processed_nodes = set()  # 已完成固定处理的节点，避免 S02 污染后站
        self._score_farm_mode = False  # S02 锁死/RUSH 后只抢任务分，不再奔终点
        self._s02_won_window = False   # 我方赢下 S02 窗口：处理完必须抢 S10
        self._rush_tactic_tried = False
        self._delivery_committed = False  # 离墙收官后一律去宫门/终点，禁止回头
        self._rolling_wall = False     # 破墙后按2621纪律滚动，普通点只下免费卡

    # ================= 初始化 =================

    def on_start(self, state):
        # 平台 start 消息里位置/障碍数据可能不全（实战 0/8 小队实锤：
        # 计划在这里建成空表且永不重建）——只做日志，计划延迟到首帧
        # inquire（state 完整）再建，见 decide() 的 _build_plans
        if self.log:
            self.log.info("warden: on_start (plans deferred to first frame)")

    def _weather_edge_cost(self, state):
        """真实移动边权：只把天气移动税计入最快路/死线账本。"""
        return self.planner._time_edge_cost_fn(state)

    def _shortest(self, state, src, dst, speed=None):
        return state.graph.shortest_path(
            src, dst, speed or P.BASE_SPEED, None,
            self._weather_edge_cost(state))

    def _shortest_avoiding(self, state, src, dst, blocked, speed=None):
        """不经过 blocked 的最短路；用于给对手的真实改道代价定价。"""
        if not src or not dst or src == blocked or dst == blocked:
            return float("inf"), []
        cost, path = state.graph.shortest_path(
            src, dst, speed or P.BASE_SPEED,
            lambda node_id: 100000 if node_id == blocked else 0,
            self._weather_edge_cost(state))
        if blocked in path:
            return float("inf"), []
        return cost, path

    def _next_hop(self, state, src, dst, speed=None):
        return state.graph.next_hop(
            src, dst, speed or state.my_speed(), None,
            self._weather_edge_cost(state))

    def _timed_path(self, state, src, dst):
        """按真实到达时间选路，固定处理站也属于路线成本。"""
        boost_type, boost_rem, _ = self._active_speed_buff(state)
        return self._travel_dynamic(
            state, src, dst, boost_type, boost_rem,
            include_intermediate_process=True,
            conservative_weather=True)

    def _timed_next_hop(self, state, src, dst):
        _, path = self._timed_path(state, src, dst)
        return path[1] if len(path) > 1 else None

    def _strategic_start_hop(self, state, cur, target):
        """起点冲主墙时把可见障碍税计入路线选择。

        直接最快路无障碍时维持原选择；只有山线等直达路线存在障碍，且
        经 S02 的完整路线（含固定处理站和自身障碍）含税更快时才改走
        S02。这样不会把 S02 偏好扩散到隐藏图的中后段决策。
        """
        if cur != state.start_node or target != self.camp_node \
                or cur == "S02" or not state.node("S02"):
            return None
        direct_eta, direct_path = self._timed_path(state, cur, target)
        if not direct_path or "S02" in direct_path:
            return None
        direct_obstacles = sum(
            1 for nid in direct_path[1:] if state.has_obstacle(nid))
        if not direct_obstacles:
            return None

        to_s02, first = self._travel_dynamic(
            state, cur, "S02", conservative_weather=True)
        if not first:
            return None
        via_eta, tail = self._travel_dynamic(
            state, "S02", target, start_elapsed=to_s02,
            include_current_process=True,
            include_intermediate_process=True,
            conservative_weather=True)
        if not tail:
            return None
        via_path = first[:-1] + tail
        via_obstacles = sum(
            1 for nid in via_path[1:] if state.has_obstacle(nid))
        forced_tax = 8
        direct_total = direct_eta + direct_obstacles * forced_tax
        via_total = via_eta + via_obstacles * forced_tax
        if via_total > direct_total + self.START_ROUTE_TAX_PAD:
            return None
        return first[1] if len(first) > 1 else None

    def _build_plans(self, state):
        self.camp_node = self._pick_camp(state)
        me_pos = state.me.get("nextNodeId") \
            if state.me.get("routeEdgeId") else state.me.get("currentNodeId")
        path = []
        if me_pos:
            _, path = self._timed_path(state, me_pos, self.camp_node)
        self._clear_plan = [n for n in (path or []) if state.has_obstacle(n)]
        if self.camp_node not in self._clear_plan \
                and state.has_obstacle(self.camp_node):
            self._clear_plan.append(self.camp_node)
        # 沿途固定处理站全部预标（-3 帧/站）：路线上处理帧 >0 的非验核站
        self._scout_plan = [
            n for n in (path or [])
            if n != me_pos
            and state.node(n).get("processType")
            and state.node(n).get("processType") != "VERIFY"
            and (state.node(n).get("processRound") or 0) > 0]
        if self.log:
            self.log.info("warden: camp=%s clear_plan=%r scout_plan=%r",
                          self.camp_node, self._clear_plan, self._scout_plan)

    def _pick_camp(self, state):
        """关键关隘里挑在我方去宫门最短路上的那个；缺失回退 S10。"""
        if self._forced_camp:
            return self._forced_camp
        me_pos = state.me.get("currentNodeId")
        path = []
        if me_pos:
            _, path = self._shortest(state, me_pos, state.gate_node)
            path = path or []
        for nid in path:
            if state.node(nid).get("nodeType") == "KEY_PASS":
                return nid
        for nid, node in state.nodes.items():
            if node.get("nodeType") == "KEY_PASS":
                return nid
        return "S10"

    def force_camp(self, node_id):
        """由混合策略指定已通过拓扑证明的墙点，并重建路线侧计划。"""
        self._forced_camp = node_id
        self.camp_node = node_id
        self._plans_ready = False
        self._delivery_committed = False
        self._rolling_wall = False

    # ================= 每帧入口 =================

    def decide(self, state):
        self._absorb_feedback(state)
        me = state.me
        if not me or me.get("delivered") or me.get("retired"):
            return []
        if not self._plans_ready and me.get("currentNodeId"):
            # 首帧建计划（不吃 start 消息的不全数据）：清障/标记/关隘一次成型
            self._plans_ready = True
            self._build_plans(state)
        self._maybe_fallback_gate(state)

        actions = []
        contests = state.my_open_contests()
        if contests:
            c = self._priority_contest(contests)
            actions.append(P.a_window_card(
                c["contestId"], self._defense_card(state, c)))

        main = self.main_action(state)
        if main:
            actions.append(main)

        squad = self.squad_action(state)
        if squad:
            actions.append(squad)

        # 移动中若同帧只有小分队/窗口动作，补显式 MOVE 保持推进
        # （服务端实测：纯辅动作包会暂停本帧推进）；目标被敌卡冻结时不补
        if (actions and me.get("state") == P.ST_MOVING and me.get("nextNodeId")
                and not state.enemy_guard(me["nextNodeId"])
                and not any(a["action"] in P.MAIN_ACTION_TYPES
                            for a in actions)):
            actions.append(P.a_move(me["nextNodeId"]))
        return actions

    def _absorb_feedback(self, state):
        super()._absorb_feedback(state)
        for e in state.my_events("DOCK_CONTEST_WIN", "WINDOW_CONTEST_END",
                                 "PROCESS_COMPLETE", "PROCESS_COMPLETED",
                                 "VERIFY_GATE_COMPLETE",
                                 "VERIFY_GATE_COMPLETED"):
            p = e.get("payload") or {}
            target = p.get("targetNodeId") or p.get("nodeId")
            etype = e.get("type")
            if target and etype in ("PROCESS_COMPLETE", "PROCESS_COMPLETED"):
                self._processed_nodes.add(target)
                if target == "S02":
                    self._processed_here = True
            if target == "S02" and etype == "DOCK_CONTEST_WIN" \
                    and p.get("playerId") == state.player_id:
                self._s02_won_window = True
            if etype == "WINDOW_CONTEST_END" \
                    and p.get("winnerPlayerId") == state.player_id \
                    and (target == "S02"
                         or p.get("contestType") == P.CONTEST_DOCK):
                self._s02_won_window = True

    def _maybe_fallback_gate(self, state):
        """当前墙被买穿后，沿双方后续路线滚动到下一个可抢截击点。"""
        if self._delivery_committed:
            return
        camp = self.camp_node
        gate = state.gate_node
        if not camp or camp == gate:
            return
        opp = state.opp
        if not opp or opp.get("delivered") or opp.get("retired"):
            return
        proc = opp.get("currentProcess") or {}
        forcing = (opp.get("state") == P.ST_FORCED_PASSING
                   or (proc.get("action") or proc.get("type")) == "FORCED_PASS") \
            and (proc.get("targetNodeId") in (camp, None))
        breaking = (proc.get("targetNodeId") == camp
                    and (proc.get("action") or proc.get("type"))
                    == "BREAK_GUARD")
        pos = opp.get("nextNodeId") or opp.get("currentNodeId")
        breached = False
        if pos and pos != camp:
            _, pth = self._shortest(state, pos, gate)
            breached = bool(pth) and camp not in pth   # 它去宫门已不经过关隘
        if forcing:
            self._last_inbound = state.round   # 转运=正在逼近，别当埋伏流
        if forcing or breaking or breached:
            next_wall = self._next_intercept_after(state, camp) or gate
            if self.log:
                self.log.info("warden: rolling wall %s -> %s (%s)", camp,
                              next_wall,
                              "forced-pass" if forcing else
                              ("break-guard" if breaking else "breached"))
            self.camp_node = next_wall
            self._rolling_wall = True

    def _next_intercept_after(self, state, old_camp):
        """2621 式转场：优先抢下一汇合点，不机械跳到 S14。"""
        gate = state.gate_node
        opp = state.opp
        opp_pos = opp.get("nextNodeId") or opp.get("currentNodeId")
        if not opp_pos:
            return gate
        _, my_path = self._shortest(state, old_camp, gate, P.SPEED_RUSH)
        opp_eta, opp_path = self._shortest(
            state, opp_pos, gate, P.SPEED_RUSH)
        if not my_path or not opp_path:
            return gate
        for node_id in my_path[1:-1]:
            my_eta, _ = self._shortest(
                state, old_camp, node_id, state.my_speed())
            their_eta, their_path = self._shortest(
                state, opp_pos, node_id, P.SPEED_RUSH)
            if not their_path or my_eta + self.MOBILE_GUARD_PAD > their_eta:
                continue
            bypass_eta, bypass_path = self._shortest_avoiding(
                state, opp_pos, gate, node_id, P.SPEED_RUSH)
            detour = (bypass_eta - opp_eta) if bypass_path else 999
            if node_id in opp_path or detour >= self.MOBILE_GUARD_MIN_DELAY:
                return node_id
        return gate

    # ================= 窗口防守 =================

    @staticmethod
    def _priority_contest(contests):
        """通行窗口（我们的墙正被强通）优先出牌。"""
        return min(contests, key=lambda c: 0 if c.get("contestType")
                   == P.CONTEST_PASS else 1)

    def _defense_card(self, state, contest):
        me = state.me
        res = me.get("resources") or {}
        # 码头窗（S02 开局争先手）：双方此时都无马 → 强行不存在 →
        # 鲜供不败（对鲜供平、对其余全胜）。1 好果/拍；若演化成镜像
        # 锁死，未交付世界里好果一文不值，烧果免费。鲜供需要鲜度≥80；
        # 跌破后继续提交会被 RESOURCE_NOT_ENOUGH 拒绝，改用兵争/弃权。
        if contest.get("contestType") in (P.CONTEST_DOCK, P.CONTEST_TASK) \
                and self._xian_gong_available(state):
            return P.CARD_XIAN_GONG
        if contest.get("contestType") in (P.CONTEST_DOCK, P.CONTEST_TASK):
            if (me.get("guardActionPoint") or 0) > 0:
                return P.CARD_BING_ZHENG
            return P.CARD_ABSTAIN
        pool = []
        if (me.get("guardActionPoint") or 0) > 0:
            pool.append(P.CARD_BING_ZHENG)   # 克强行（攻方骑马强通的主牌）
        if contest.get("contestType") == P.CONTEST_PASS:
            # 守墙拍才烧硬通货：鲜供克兵争/验牒
            if me.get("freshness", 0) >= 80 \
                    and me.get("goodFruit", 0) > self.FRUIT_RESERVE:
                pool.append(P.CARD_XIAN_GONG)
        if res.get(P.PASS_TOKEN, 0) + res.get(P.OFFICIAL_PERMIT, 0) > 0:
            pool.append(P.CARD_YAN_DIE)
        if state.has_move_buff():
            pool.append(P.CARD_QIANG_XING)
        if not pool:
            return P.CARD_ABSTAIN
        # 按拍序轮转，防被读死同一张
        return pool[state.round % len(pool)]

    # ================= 主车队 =================

    def main_action(self, state):
        me = state.me
        if me.get("state") in P.BUSY_STATES:
            return None
        camp = self.camp_node

        if me.get("routeEdgeId"):
            # 边上：无增益就上马；被敌卡冻住由 squad_action 削弱自救
            res = me.get("resources") or {}
            nxt = me.get("nextNodeId")
            if nxt and state.enemy_guard(nxt):
                return None
            rush = self._rush_speed_action(state)
            if rush:
                return rush
            if not state.has_move_buff():
                for h in (P.FAST_HORSE, P.SHORT_HORSE):
                    if res.get(h, 0) > 0:
                        return P.a_use_resource(h)
            return None

        cur = me.get("currentNodeId")
        gate, terminal = state.gate_node, state.terminal_node
        remain = state.duration_round - state.round

        # ---- 最高优先级：我方理论上已到不了终点 → 立刻转农 ----
        # EXIT_PAD 是离墙安全垫，不是放弃交付阈值；否则到 S14 临界帧会
        # 误判成转农 WAIT，白白错过验核/交付。
        if not self._delivery_committed and not me.get("verified") \
                and remain < self._my_need(state, cur) + self.FARM_DEAD_PAD:
            self._score_farm_mode = True
            return self._farm_endgame(state, cur)

        # ---- 墙优先级：对手已经踏边时，本帧能设卡就先设卡 ----
        # 任务是墙成立后的白捡收益；不能反过来让 3-4 帧读条吃掉落卡窗口。
        if cur in (self.camp_node, gate):
            guard = self._reactive_guard(state, cur)
            if guard:
                return guard

        # 2621 式滚动截击：对手已经用 routeEdgeId/nextNodeId 公开承诺路线，
        # 我方恰好占住其目标点时，先落最低成本有效卡，再做处理/任务。
        # 这层不猜画像，也不预卡；到不了终点或已进入收官时完全关闭。
        guard = self.mobile_intercept_action(state)
        if guard:
            return guard

        # ---- 农任务终局（S02 镜像锁死等场景）----
        # 我方交付已不可能：分数只剩未交付任务分可挣。但对手还活着时
        # 必须继续争（让行=放它出去交付）；对手也死了才让行转农
        if cur == "S02" and self._s02_won_window \
                and self._node_processed(state, cur):
            return self._advance(state, cur, camp)
        if cur == "S02" and not self._node_processed(state, cur) \
                and self._s02_lock_spent(state):
            self._score_farm_mode = True
            return self._farm_endgame(state, cur)
        if self._score_farm_mode and not me.get("verified"):
            return self._farm_endgame(state, cur)
        if self._s02_lock_hold(state, cur):
            node = state.node(cur)
            needs = (node.get("processType")
                     and node.get("processType") != "VERIFY"
                     and node.get("processRound", 0) > 0)
            if needs and not self._node_processed(state, cur):
                return P.a_process()
            return P.a_wait()

        # ---- 交付线 ----
        if cur == terminal:
            if me.get("verified") and me.get("goodFruit", 0) > 0 \
                    and me.get("freshness", 0) > 0:
                return P.a_deliver()
            return P.a_wait()
        if me.get("verified"):
            if cur == gate and self.camp_node == gate \
                    and not self._should_leave(state, cur):
                guard = self._reactive_guard(state, cur)
                if guard:
                    return guard
                task = self._farm_here_safe(state, cur)
                if task:
                    return task
                return P.a_wait()      # 已验核仍守宫门：墙焊到死线再走
            if cur == gate and self.camp_node == gate:
                guard = self._depart_guard(state, cur)
                if guard:
                    return guard
            return self._advance(state, cur, terminal)
        if cur == gate:
            if self.camp_node == gate:
                guard = self._reactive_guard(state, cur)
                if guard:
                    return guard
                task = self._farm_here_safe(state, cur)
                if task:
                    return task
            if state.phase == P.PHASE_RUSH:
                return P.a_verify_gate()
            return P.a_wait()

        # ---- 固定处理站（途中驿站/码头/水驿必须处理完才能走）----
        node = state.node(cur)
        needs = (node.get("processType") and node.get("processType") != "VERIFY"
                 and node.get("processRound", 0) > 0)
        if needs and not self._node_processed(state, cur):
            proc = (state.opp.get("currentProcess") or {})
            if proc.get("targetNodeId") == cur:
                return P.a_wait()            # 排队，别开无谓的码头窗口
            return P.a_process()

        # ---- 已离墙收官：这是单向阶段，不能因为下一帧余量变化又回 S10 ----
        if self._delivery_committed:
            rush = self._rush_speed_action(state)
            if rush:
                return rush
            return self._advance(state, cur, gate)

        handoff_task = self._handoff_farm_action(state, cur)
        if handoff_task:
            return handoff_task
        old_camp = self.camp_node
        leaving = self._should_leave(state, cur)
        if leaving and cur == old_camp:
            self._delivery_committed = True

        # ---- 竞速段：直奔关隘 ----
        if cur != camp and not leaving:
            claim = self._claim_en_route(state, cur)
            if claim:
                return claim
            return self._advance(state, cur, camp)

        # ---- 封锁段：驻守 ----
        if cur == camp and not leaving:
            guard = self._reactive_guard(state, cur)
            if guard:
                return guard
            # 任务读条 3-4 帧不可打断：对手逼近到"读条期内会上边+进关"
            # 的距离时不开新任务，保持空闲随时可拦（实战：读条锁身位
            # 放它溜进关）
            opp = state.opp
            near = False
            if opp and not opp.get("delivered") and not opp.get("retired"):
                pos = opp.get("nextNodeId") or opp.get("currentNodeId")
                if pos:
                    oeta, op = self._shortest(state, pos, cur)
                    near = bool(op) and oeta <= 12   # 读条4+设卡5+余量
            if (not near) or self._my_active_guard(state, cur):
                task = self._farm_here_safe(state, cur)
                if task:
                    return task
            return P.a_wait()

        # ---- 收官段 ----
        if cur == camp:
            guard = self._depart_guard(state, cur)
            if guard:
                return guard
        rush = self._rush_speed_action(state)
        if rush:
            return rush
        return self._advance(state, cur, gate)

    def _advance(self, state, cur, target):
        """朝 target 走一步。竞速/封锁期强通免冻，转农期不为刷分强通。"""
        me = state.me
        nxt = self._strategic_start_hop(state, cur, target) \
            or self._timed_next_hop(state, cur, target)
        if nxt is None:
            return P.a_wait()
        g = state.enemy_guard(nxt)
        if g:
            if self._score_farm_mode:
                return P.a_wait()
            good, bad = me.get("goodFruit", 0), me.get("badFruit", 0) or 0
            gf = min(2, max(0, good - self.FRUIT_RESERVE))
            bf = min(2, bad)
            if gf * 2 + bf * 3 >= (g.get("defense", 0) or 0):
                return P.a_break_guard(nxt, gf, bf)
            return P.a_forced_pass(nxt)
        if state.has_obstacle(nxt):
            if self._score_farm_mode:
                return P.a_wait()
            return P.a_forced_pass(nxt)      # 固定 8 帧税，无窗口，免冻
        return P.a_move(nxt)

    def _claim_en_route(self, state, cur):
        me = state.me
        stock = state.node(cur).get("resourceStock") or {}
        res = me.get("resources") or {}
        for rt in self.CLAIM_EN_ROUTE:
            if stock.get(rt, 0) > 0 and res.get(rt, 0) < 1:
                return P.a_claim_resource(cur, rt)
        return None

    def _s02_lock_hold(self, state, cur):
        """S02 镜像码头窗：RUSH 前目标是拖住双方，不是抢先离站。"""
        if cur != "S02" or state.phase == P.PHASE_RUSH:
            return False
        if state.me.get("verified") or self._node_processed(state, cur):
            return False
        if self._s02_lock_spent(state):
            return False
        opp = state.opp
        return bool(opp and not opp.get("delivered") and not opp.get("retired")
                    and not opp.get("routeEdgeId")
                    and opp.get("currentNodeId") == cur)

    @staticmethod
    def _xian_gong_available(state):
        me = state.me
        return bool(me.get("freshness", 0) >= 80
                    and me.get("goodFruit", 0) > 1)

    def _node_processed(self, state, node_id=None):
        node_id = node_id or state.me.get("currentNodeId")
        if not node_id:
            return False
        # 兼容旧状态钉子：_processed_here 只代表 S02，不再代表任意处理站。
        return node_id in self._processed_nodes \
            or (node_id == "S02" and self._processed_here)

    def _s02_lock_spent(self, state):
        """S02 假锁识别：献贡/兵争都不可用或 RUSH 已到，才停止开窗。"""
        if state.phase == P.PHASE_RUSH:
            return True
        if self._xian_gong_available(state):
            return False
        return (state.me.get("guardActionPoint") or 0) <= 0

    # ---- 封锁 ----

    def _my_eta(self, state, node_id):
        """我方到目标节点的帧数，含路线边上剩余进度（同 planner._opp_eta 口径）。"""
        me = state.me
        edge_remain = 0.0
        if me.get("routeEdgeId") and me.get("nextNodeId"):
            total = me.get("edgeTotalMs") or 0
            done = me.get("edgeProgressMs") or 0
            edge_remain = max(0, total - done) / 1000.0
            pos = me.get("nextNodeId")
        else:
            pos = me.get("currentNodeId")
        if not pos:
            return float("inf")
        f, path = self._shortest(state, pos, node_id, state.my_speed())
        return edge_remain + f if path else float("inf")

    @staticmethod
    def _has_our_mark(state, node_id):
        for m in state.node(node_id).get("scouted") or []:
            if m.get("teamId") == state.my_team \
                    and m.get("remainingTriggers", 1) > 0:
                return True
        return False

    def _my_active_guard(self, state, node_id):
        g = state.node(node_id).get("guard")
        return bool(g and g.get("ownerTeamId") == state.my_team
                    and g.get("active", g.get("defense", 0) > 0))

    def _my_guard_defense(self, state, node_id):
        g = state.node(node_id).get("guard")
        if not (g and g.get("ownerTeamId") == state.my_team
                and g.get("active", g.get("defense", 0) > 0)):
            return 0
        return g.get("defense", 0) or 0

    @staticmethod
    def _guard_base_cost(state, node_id):
        return 1 if state.node(node_id).get("nodeType") \
            in ("KEY_PASS", "GATE") else 0

    def _guardable(self, state, node_id, extra=None, mobile=False,
                   allow_reserve=False):
        """该节点当前无有效卡，成本、重试间隔均允许。"""
        g = state.node(node_id).get("guard")
        if g and g.get("active", g.get("defense", 0) > 0):
            return False
        if extra is None:
            extra = 1 if node_id == state.gate_node else self.GUARD_EXTRA
        cost = self._guard_base_cost(state, node_id) + extra
        reserve = 0 if allow_reserve else self.FRUIT_RESERVE
        if (state.me.get("goodFruit", 0) or 0) - cost < reserve:
            return False
        gap = self.MOBILE_GUARD_RETRY_GAP if mobile \
            else self.GUARD_RETRY_GAP
        return state.round - self._guard_sent.get(node_id, -999) \
            >= gap

    def _opp_inbound(self, state, node_id):
        """对手已踏上通往 node_id 的边（虚卡转实卡的唯一触发）。"""
        opp = state.opp
        if not opp or opp.get("delivered") or opp.get("retired"):
            return False
        hit = bool(opp.get("routeEdgeId")
                   and opp.get("nextNodeId") == node_id)
        if hit:
            self._last_inbound = state.round
        return hit

    def _opp_near(self, state, node_id, eta_cap):
        opp = state.opp
        if not opp or opp.get("delivered") or opp.get("retired"):
            return False
        pos = opp.get("nextNodeId") or opp.get("currentNodeId")
        if not pos:
            return False
        eta, path = self._shortest(state, pos, node_id)
        return bool(path) and eta <= eta_cap

    # 提交 T -> T+4 完成 -> T+5 起拦；剩余 5 帧即可拦。
    GUARD_MIN_LEAD = 5

    def _opp_edge_remaining(self, state):
        """对手当前已承诺边的乐观剩余帧，显式计可见马/疾行速度。"""
        opp = state.opp
        total = opp.get("edgeTotalMs") or 0
        done = opp.get("edgeProgressMs") or 0
        _, _, speed = self._active_speed_buff(state, opp, optimistic=True)
        if state.phase == P.PHASE_RUSH \
                and not (opp.get("rushTacticUsedCount") or 0) \
                and (opp.get("goodFruit", 0) or 0) >= 3:
            speed = max(speed, P.SPEED_RUSH)
        return int(math.ceil(max(0, total - done) / max(1, speed)))

    def _mobile_guard_extra(self, state, node_id):
        """复刻 2621 的资源纪律：普通点免费防2，关键点才花果。"""
        node_type = state.node(node_id).get("nodeType")
        if node_id == state.gate_node or node_type == "GATE":
            return 1                       # 宫门上限防4，extra=1 不溢出
        if node_type == "KEY_PASS":
            return 1                       # 防4比防6少一篓，首风化仍是45帧
        return 0                           # 普通节点免费防2，可反复滚动截击

    def _mobile_guard_delay(self, state, node_id, extra, edge_remain):
        """对手在“等卡/拆卡”和“中途改道”中选更快者后的保底延误。"""
        opp = state.opp
        origin = opp.get("currentNodeId")
        gate = state.gate_node
        if not origin or not gate:
            return 0

        stay_delay = self._mobile_stay_delay(
            state, node_id, extra, edge_remain)
        direct_after, direct_path = self._shortest(
            state, node_id, gate, P.SPEED_RUSH)
        alt, alt_path = self._shortest_avoiding(
            state, origin, gate, node_id, P.SPEED_RUSH)
        if not direct_path:
            return 0
        direct = edge_remain + direct_after
        reroute_delay = max(0, alt - direct) if alt_path else 999
        return min(stay_delay, reroute_delay)

    def _mobile_stay_delay(self, state, node_id, extra, edge_remain):
        """卡在对手到站时剩余的阻塞时间，含其公开小分队拆卡上界。"""
        opp = state.opp
        node_type = state.node(node_id).get("nodeType")
        defense = min(4 if node_type == "GATE" else 7,
                      2 + 2 * max(0, extra))
        first_weather = 45 if node_type == "KEY_PASS" and defense >= 4 else 30
        lifetime = first_weather + max(0, defense - 1) * 30
        stay_delay = max(0, lifetime - max(0, edge_remain - 4))

        # 非 RUSH 时小分队可每次削 2 防；把公开弹药纳入“留下来拆”的上界。
        squads = opp.get("squadAvailable")
        if squads is None:
            squads = 8  # 字段缺失按最强拆卡能力估计，不能误证“锁死”
        dispatches = int(math.ceil(defense / 2.0))
        if state.phase != P.PHASE_RUSH and squads is not None \
                and squads >= dispatches * 2:
            clear_time = 8 + max(0, dispatches - 1) * self.WEAKEN_RESEND_GAP
            stay_delay = min(stay_delay, clear_time)
        return stay_delay

    def mobile_intercept_action(self, state, delivery_slack=None,
                                allow_reserve=False):
        """对手踏向我方所在节点时，按确定性收益落一张滚动截击卡。

        可供 Hybrid 的 SCORE 模式复用；不改变传统规划器的其余动作。
        """
        me = state.me
        if not me or me.get("state") in P.BUSY_STATES \
                or me.get("routeEdgeId") or self._score_farm_mode \
                or self._delivery_committed or me.get("verified"):
            return None
        if state.my_open_contests():
            return None
        cur = me.get("currentNodeId")
        # S02 完整保留 3.96.34 的窗口/换乘博弈，移动截击不得抢优先级。
        if not cur or cur == "S02" \
                or cur in (self.camp_node, state.gate_node,
                              state.terminal_node):
            return None
        if not self._opp_inbound(state, cur):
            return None

        edge_remain = self._opp_edge_remaining(state)
        if edge_remain < self.MOBILE_GUARD_PAD:
            return None
        slack = self._departure_slack(state, cur) \
            if delivery_slack is None else delivery_slack
        if slack < self.MOBILE_GUARD_PAD:
            return None

        extra = self._mobile_guard_extra(state, cur)
        if not self._guardable(
                state, cur, extra=extra, mobile=True,
                allow_reserve=allow_reserve):
            return None
        delay = self._mobile_guard_delay(state, cur, extra, edge_remain)
        if delay < self.MOBILE_GUARD_MIN_DELAY:
            return None

        self._guard_sent[cur] = state.round
        if self.log:
            self.log.info("warden: mobile intercept @%s extra=%d delay>=%.0f",
                          cur, extra, delay)
        return P.a_set_guard(cur, extra)

    def _reactive_guard(self, state, cur):
        """虚卡纪律：对手上边才落卡；卡亡且它仍在边上 → 立即补。

        来得及才立（实战 r256 教训：对手 2 帧后进站，读条 4 帧的卡
        r260 才成型，白烧 3 好果拦了个寂寞）。"""
        if not self._opp_inbound(state, cur):
            return None
        # 宫门卡只是最后保险，不能反过来吃掉我方交付安全垫。第一墙仍按
        # 原守望者纪律焊死；只有 S14 在设卡处理来不及装进余量时放弃卡。
        if cur == state.gate_node \
                and self._departure_slack(state, cur) < self.GUARD_MIN_LEAD:
            return None
        if self._opp_edge_remaining(state) < self.GUARD_MIN_LEAD:
            return None
        extra = 1 if cur == state.gate_node else self.GUARD_EXTRA
        rolling = self._rolling_wall and cur != state.gate_node
        if rolling:
            extra = self._mobile_guard_extra(state, cur)
        if not self._guardable(state, cur, extra=extra, mobile=rolling):
            return None
        self._guard_sent[cur] = state.round
        if self.log:
            self.log.info("warden: reactive guard @%s (opp inbound)", cur)
        return P.a_set_guard(cur, extra)

    def _depart_guard(self, state, cur):
        """临别卡：离场时对手仍在逼近就再挡一手。"""
        if self._my_active_guard(state, cur):
            return None
        if cur == state.gate_node and not self._opp_inbound(state, cur):
            return None
        if not (self._opp_inbound(state, cur)
                or self._opp_near(state, cur, self.THREAT_ETA)):
            return None
        extra = 1 if cur == state.gate_node else self.GUARD_EXTRA
        rolling = self._rolling_wall and cur != state.gate_node
        if rolling:
            extra = self._mobile_guard_extra(state, cur)
        if not self._guardable(state, cur, extra=extra, mobile=rolling):
            return None
        self._guard_sent[cur] = state.round
        if self.log:
            self.log.info("warden: parting guard @%s", cur)
        return P.a_set_guard(cur, extra)

    def _farm_here(self, state, cur):
        me = state.me
        res = me.get("resources") or {}
        has_horse = any(res.get(h, 0) > 0
                        for h in (P.FAST_HORSE, P.SHORT_HORSE))
        for t in state.claimable_tasks():
            if t.get("nodeId") != cur:
                continue
            if self._farm_task_blocked(state, t, has_horse):
                continue
            return P.a_claim_task(t["taskId"])
        return self._claim_en_route(state, cur)

    def _farm_task_blocked(self, state, task, has_horse):
        """刷分模式只吃确定净赚任务，不为清障/障碍点烧主车强通税。"""
        tpl = task.get("taskTemplateId")
        if tpl == "T04" or (tpl == "T06" and not has_horse):
            return True
        labels = (tpl, task.get("name"), task.get("taskName"),
                  task.get("taskTemplateName"), task.get("templateName"))
        if any("清障" in str(x) for x in labels if x):
            return True
        nid = task.get("nodeId")
        return bool(nid and state.has_obstacle(nid))

    def _farm_path_blocked(self, state, path):
        """刷分路线不走下一跳强通；宁可换桶也不把 60+ 帧砸进障碍。"""
        if not path or len(path) < 2:
            return False
        nxt = path[1]
        return bool(state.has_obstacle(nxt) or state.enemy_guard(nxt))

    # ---- 农任务终局 ----

    def _at_contested_station(self, state, cur):
        """还站在与对手争夺中的未处理站上（懦夫博弈仍在进行）。"""
        node = state.node(cur)
        opp = state.opp
        return bool(node.get("processType")
                    and (node.get("processRound") or 0) > 0
                    and not self._node_processed(state, cur)
                    and opp and opp.get("currentNodeId") == cur)

    def _opp_alive_can_deliver(self, state, remain):
        """还需要用争夺/驻守拖住对手吗？对手已交付/退赛/数学死=不需要。"""
        opp = state.opp
        if not opp or opp.get("delivered") or opp.get("retired"):
            return False
        pos = opp.get("nextNodeId") or opp.get("currentNodeId")
        if not pos:
            return True
        need = self._delivery_need(state, pos, P.BASE_SPEED,
                                   move_factor=self.OPP_SPEED_MARGIN)
        if need >= 999:
            return False
        return need <= remain      # 零缓冲：它数学死即死，秒让抢农起跑

    def _farm_endgame(self, state, cur):
        """交付双死后的任务分收割：让行出站 → 追最近可达任务 →
        小分队全转探路标记提速领取。"""
        me = state.me
        node = state.node(cur)
        needs = (node.get("processType") and node.get("processType") != "VERIFY"
                 and node.get("processRound", 0) > 0)
        if needs and not self._node_processed(state, cur):
            if cur == "S02":
                proc = (state.opp.get("currentProcess") or {})
                if proc.get("targetNodeId") == cur:
                    return P.a_wait()
                return P.a_process()
            # 奇偶让行防镜像双让锁死：一方先起手，另一方吃 OBJECT_BUSY 排队
            opp = state.opp
            opp_here = (opp and not opp.get("routeEdgeId")
                        and opp.get("currentNodeId") == cur
                        and not (opp.get("currentProcess") or {}))
            # 让行期顺手标脚下（处理 4→2，后手赤字 9→4）
            self._farm_target = cur
            # 让行边：pid 小的偶帧起手、pid 大的奇帧——互补保证不撞车
            side = 0 if state.player_id < (state.opp_id or 0) else 1
            if opp_here and state.round % 2 != side:
                return P.a_wait()
            proc = (state.opp.get("currentProcess") or {})
            if proc.get("targetNodeId") == cur:
                return P.a_wait()
            return P.a_process()
        # 脚下资源：农期马就是任务巡回速度（短马也领）
        stock = state.node(cur).get("resourceStock") or {}
        res2 = state.me.get("resources") or {}
        for rt in (P.FAST_HORSE, P.SHORT_HORSE):
            if stock.get(rt, 0) > 0 and res2.get(rt, 0) < 1:
                return P.a_claim_resource(cur, rt)
        # 脚下任务直接吃
        task = self._farm_here(state, cur)
        if task:
            return task
        # 现存赶得上的任务优先；没有就驻守刷新候选点等波次（刷新帧
        # r360/420/480 可预测；实战 vs2769 教训：追尸体到点全过期 +
        # 天气边 113 帧 + 干等 54 帧 = 0 任务）
        best, best_rank = None, None
        res = me.get("resources") or {}
        has_horse = any(res.get(h, 0) > 0
                        for h in (P.FAST_HORSE, P.SHORT_HORSE))
        opp = state.opp
        opp_pos = opp and (opp.get("nextNodeId") or opp.get("currentNodeId"))
        for t in state.claimable_tasks():
            if self._farm_task_blocked(state, t, has_horse):
                continue
            eta, path = self._shortest(state, cur, t["nodeId"],
                                       state.my_speed())
            if not path:
                continue
            if self._farm_path_blocked(state, path):
                continue
            if self._farm_backtrack_step(state, cur, path):
                continue
            proc = t.get("processRound", 4) or 4
            expire = t.get("expireRound") or 0
            if expire and state.round + eta + proc + 2 > expire:
                continue
            if state.round + eta + proc > state.duration_round:
                continue
            ev = self._farm_task_expected_value(state, t, eta, proc, opp_pos)
            ev += self._farm_followup_value(
                state, t, eta, proc, opp_pos, has_horse)
            if ev <= 0:
                continue          # 它必先完成：别追尸体，换线抢别的桶
            back = self._farm_backtrack_step(state, cur, path)
            busy = max(1, eta + proc)
            rank = (1 if back else 0, -ev / busy, -ev, eta)
            if best_rank is None or rank < best_rank:
                best, best_rank = t["nodeId"], rank
        if best is None:
            # 无可追实例 → 驻守刷新候选点吃下一波。分桶原则：先手 4 帧
            # 只在争同一实例时值钱——只选"我比对手近"的点，它先出发
            # 的先手作用于我们不去的桶，赤字归零；全被占回退任意最近
            cands = set()
            for nodes in (state.task_candidates or {}).values():
                cands.update(nodes)
            for t in state.tasks:
                nid = t.get("nodeId")
                if nid:
                    cands.add(nid)
            for nid, node in state.nodes.items():
                if self._farm_visible_resource_value(state, nid) > 0:
                    cands.add(nid)
            fb, fb_rank = None, None
            for nid in cands:
                if nid == cur and len(cands) > 1:
                    continue
                eta, path = self._shortest(state, cur, nid,
                                           state.my_speed())
                if not path:
                    continue
                if self._farm_path_blocked(state, path):
                    continue
                if state.round + eta > state.duration_round:
                    continue
                if self._farm_backtrack_step(state, cur, path):
                    continue
                value = (FARM_REFRESH_VALUE
                         + self._farm_visible_resource_value(state, nid)
                         + self._farm_bucket_value(state, cur, nid, eta,
                                                   path, opp_pos))
                busy = max(1, eta)
                rank = (-value / busy, -value, eta)
                if fb_rank is None or rank < fb_rank:
                    fb, fb_rank = nid, rank
                if opp_pos:
                    oeta, opath = self._shortest(state, opp_pos, nid)
                    if opath and oeta <= eta + FARM_TASK_RACE_MARGIN:
                        continue          # 它更近：优先换桶，别追尾
                if best_rank is None or rank < best_rank:
                    best, best_rank = nid, rank
            if best is None:
                best, best_rank = fb, fb_rank
        self._farm_target = best
        if best and best != cur:
            return self._advance(state, cur, best)
        return P.a_wait()   # 已在候选点上：守波次，脚下刷出即被 _farm_here 吃

    def _farm_backtrack_step(self, state, cur, path):
        """转农后避免 S03->S02 这类向起点折返。"""
        if not path or len(path) < 2:
            return False
        here = state.node(cur)
        nxt = state.node(path[1])
        hx, nx = here.get("x", here.get("X")), nxt.get("x", nxt.get("X"))
        if hx is None or nx is None:
            return False
        return nx < hx

    def _farm_visible_resource_value(self, state, node_id):
        stock = state.node(node_id).get("resourceStock") or {}
        res = state.me.get("resources") or {}
        value = 0
        for rt, v in FARM_RESOURCE_VALUE.items():
            if stock.get(rt, 0) > 0 and res.get(rt, 0) < 1:
                value += v
        return value

    def _farm_node_bucket(self, state, origin, node_id, path=None):
        """转农路线桶：优先用任务/节点语义，其次看路径上首个非普通特征。"""
        for t in state.tasks:
            if t.get("nodeId") == node_id:
                bucket = t.get("routeBucket") or t.get("routeType")
                if bucket:
                    return bucket
        node = state.node(node_id)
        ntype = node.get("nodeType") or node.get("type")
        ptype = node.get("processType") or ""
        if ntype in ("DOCK", "WATER_STATION") or "船" in str(ptype) \
                or "水" in str(ptype):
            return P.WATER
        if path is None:
            _, path = self._shortest(state, origin, node_id,
                                     state.my_speed())
        path = path or ()
        seen = None
        for a, b in zip(path, path[1:]):
            edge = state.graph.edge_between(a, b)
            rt = edge.get("routeType") if edge else None
            if rt in (P.WATER, P.MOUNTAIN, P.BRANCH):
                return rt
            if rt:
                seen = seen or rt
        return seen

    def _farm_opp_bucket(self, state, cur):
        opp = state.opp or {}
        if not opp or opp.get("delivered") or opp.get("retired"):
            return None
        pos = opp.get("nextNodeId") or opp.get("currentNodeId")
        if pos:
            bucket = self._farm_node_bucket(state, cur, pos)
            if bucket:
                return bucket
        edge_id = opp.get("routeEdgeId")
        if edge_id and edge_id in state.graph.edges:
            return state.graph.edges[edge_id].get("routeType")
        return None

    def _farm_task_score_hint(self, state, task):
        score = task.get("score")
        if score is not None:
            return score or 0
        tpl = state.task_templates.get(task.get("taskTemplateId")) or {}
        return tpl.get("score") or tpl.get("baseScore") or 30

    def _farm_bucket_score_ceiling(self, state, bucket, origin, base_score):
        """估算某路线桶到 600 帧前还能支撑的任务分上限。"""
        if not bucket:
            return 0
        remaining = max(0, FARM_TASK_CAP - (base_score or 0))
        if remaining <= 0:
            return 0
        total = 0.0
        seen = set()
        for t in state.tasks:
            tid = t.get("taskId") or (t.get("taskTemplateId"), t.get("nodeId"))
            if tid in seen:
                continue
            seen.add(tid)
            tb = t.get("routeBucket") or t.get("routeType") \
                or self._farm_node_bucket(state, origin, t.get("nodeId"))
            if tb != bucket:
                continue
            weight = 1.0 if (t.get("active") and not t.get("completed")
                             and not t.get("failed")) \
                else FARM_BUCKET_HISTORY_WEIGHT
            total += self._farm_task_score_hint(state, t) * weight
        for tpl_id, nodes in (state.task_candidates or {}).items():
            tpl = state.task_templates.get(tpl_id) or {}
            score = tpl.get("score") or tpl.get("baseScore") or 30
            for node_id in nodes:
                tb = self._farm_node_bucket(state, origin, node_id)
                if tb == bucket:
                    total += score * FARM_BUCKET_HISTORY_WEIGHT
        return min(remaining, total)

    def _farm_bucket_value(self, state, cur, node_id, eta, path, opp_pos):
        bucket = self._farm_node_bucket(state, cur, node_id, path)
        ceiling = self._farm_bucket_score_ceiling(
            state, bucket, cur, state.me.get("taskScore", 0) or 0)
        value = min(FARM_BUCKET_VALUE_CAP, ceiling) * FARM_BUCKET_VALUE_MULT
        opp_bucket = self._farm_opp_bucket(state, cur)
        if bucket and opp_bucket == bucket and opp_pos:
            oeta, opath = self._shortest(state, opp_pos, node_id)
            if opath and oeta <= eta + FARM_TASK_RACE_MARGIN:
                opp_base = (state.opp or {}).get("taskScore", 0) or 0
                opp_cap = self._farm_bucket_score_ceiling(
                    state, bucket, opp_pos, opp_base)
                value -= FARM_SAME_BUCKET_LEAD_PENALTY \
                    + min(FARM_BUCKET_VALUE_CAP, opp_cap) \
                    * FARM_BUCKET_VALUE_MULT * 0.35
        return value

    def _farm_task_expected_value(self, state, task, my_eta, proc, opp_pos):
        base = state.me.get("taskScore", 0) or 0
        raw = min(task.get("score", 0) or 0, max(0, FARM_TASK_CAP - base))
        if raw <= 0:
            return 0
        return raw * self._farm_task_race_factor(
            state, task["nodeId"], my_eta, proc, opp_pos)

    def _farm_followup_value(self, state, first, first_eta, first_proc,
                             opp_pos, has_horse):
        """转农看一跳后继收益：600 帧分数最大化，不只贪最近一个任务。"""
        base = state.me.get("taskScore", 0) or 0
        gained = min(first.get("score", 0) or 0,
                     max(0, FARM_TASK_CAP - base))
        base_after = min(FARM_TASK_CAP, base + gained)
        if base_after >= FARM_TASK_CAP:
            return 0
        node_id = first.get("nodeId")
        arrive_round = state.round + first_eta + first_proc
        best = 0
        for t in state.claimable_tasks():
            if t.get("taskId") == first.get("taskId"):
                continue
            if self._farm_task_blocked(state, t, has_horse):
                continue
            eta, path = self._shortest(state, node_id, t["nodeId"],
                                       state.my_speed())
            if not path or self._farm_path_blocked(state, path):
                continue
            if self._farm_backtrack_step(state, node_id, path):
                continue
            proc = t.get("processRound", 4) or 4
            finish = arrive_round + eta + proc
            expire = t.get("expireRound") or 0
            if expire and finish + 2 > expire:
                continue
            if finish > state.duration_round:
                continue
            raw = min(t.get("score", 0) or 0,
                      max(0, FARM_TASK_CAP - base_after))
            if raw <= 0:
                continue
            factor = 1.0
            if opp_pos:
                oeta, opath = self._shortest(state, opp_pos, t["nodeId"])
                if opath and state.round + oeta + proc \
                        <= finish + FARM_TASK_RACE_MARGIN:
                    factor = 0.0
            best = max(best, raw * factor)
        return best * 0.55

    def _farm_task_race_factor(self, state, node_id, my_eta, proc, opp_pos):
        if not opp_pos:
            return 1.0
        oeta, opath = self._shortest(state, opp_pos, node_id)
        if not opath:
            return 1.0
        my_finish = state.round + my_eta + proc
        opp_finish = state.round + oeta + proc
        if my_finish + FARM_TASK_RACE_MARGIN < opp_finish:
            return 1.0
        if my_finish < opp_finish:
            return FARM_CONTEST_DISCOUNT
        return 0.0

    # ---- 收官判定 ----

    def _node_process_frames(self, state, node_id, include_verify=False):
        """读当前地图的固定处理帧；宫门验核单独按 include_verify 计入。"""
        node = state.node(node_id)
        proc = node.get("processRound") or 0
        if proc <= 0:
            return 0
        ptype = node.get("processType")
        is_gate = node_id == state.gate_node or ptype == "VERIFY"
        if is_gate:
            return proc if include_verify else 0
        return proc if ptype else 0

    def _node_process_frames_at(self, state, node_id, abs_round,
                                include_verify=False,
                                worst_unknown_weather=False):
        """规则口径固定处理：暴雨命中登船/水路换运时额外 +4 帧。"""
        proc = self._node_process_frames(
            state, node_id, include_verify=include_verify)
        if proc <= 0:
            return 0
        ptype = state.node(node_id).get("processType")
        if ptype not in ("BOARD", "WATER_TRANSFER"):
            return proc
        weather = self._weather_type_at(state, abs_round)
        if weather == P.HEAVY_RAIN or (worst_unknown_weather
                                      and weather is None
                                      and self._unknown_weather_possible(
                                          state, abs_round)):
            proc += 4
        return proc

    def _path_process_frames(self, state, path, include_current=False):
        """最短路上的中途固定处理站读条，随地图 JSON 动态变化。"""
        total = 0
        for i, nid in enumerate(path or []):
            if i == 0 and not include_current:
                continue
            if nid == state.gate_node or nid == state.terminal_node:
                continue
            total += self._node_process_frames(state, nid)
        return total

    def _gate_verify_frames(self, state):
        return self._node_process_frames(
            state, state.gate_node, include_verify=True) or GATE_VERIFY_FRAMES

    @staticmethod
    def _buff_remaining(buff, default=0):
        for key in ("remainRound", "remainingRound", "remain", "durationRound"):
            if buff.get(key) is not None:
                try:
                    return max(0, int(buff[key]))
                except (TypeError, ValueError):
                    return default
        return default

    def _active_speed_buff(self, state, player=None, optimistic=False):
        """当前公开移动增益。未知剩余时长：我方按 1 帧，对手乐观按满额。"""
        player = player or state.me
        best = (None, 0, P.BASE_SPEED)
        defaults = {
            P.FAST_HORSE: FAST_HORSE_FRAMES,
            P.SHORT_HORSE: SHORT_HORSE_FRAMES,
            P.RUSH_SPEED: RUSH_SPEED_FRAMES,
        }
        speeds = {
            P.FAST_HORSE: P.SPEED_FAST_HORSE,
            P.SHORT_HORSE: P.SPEED_SHORT_HORSE,
            P.RUSH_SPEED: P.SPEED_RUSH,
        }
        for b in player.get("buffs") or []:
            typ = b.get("type") or b.get("buffType")
            if typ not in speeds:
                continue
            rem = self._buff_remaining(
                b, defaults[typ] if optimistic else 1)
            if rem <= 0:
                continue
            cand = (typ, rem, speeds[typ])
            if cand[2] > best[2]:
                best = cand
        return best

    @staticmethod
    def _consume_boost(boost_type, boost_rem, frames):
        if boost_type and boost_rem > 0:
            boost_rem = max(0, boost_rem - int(frames))
            if boost_rem <= 0:
                return None, 0
        return boost_type, boost_rem

    @staticmethod
    def _boost_speed(boost_type):
        return {
            P.FAST_HORSE: P.SPEED_FAST_HORSE,
            P.SHORT_HORSE: P.SPEED_SHORT_HORSE,
            P.RUSH_SPEED: P.SPEED_RUSH,
        }.get(boost_type, P.BASE_SPEED)

    @staticmethod
    def _weather_type_at(state, abs_round):
        weather = state.weather or {}
        for w in weather.get("active") or []:
            rem = w.get("remainRound") or w.get("remainingRound") or 0
            if state.round <= abs_round < state.round + rem:
                return w.get("type")
        for w in weather.get("forecast") or []:
            start = w.get("startRound") or w.get("start")
            dur = w.get("durationRound") or w.get("duration") or 0
            if start is not None and start <= abs_round < start + dur:
                return w.get("type")
        return None

    @staticmethod
    def _unknown_weather_possible(state, abs_round):
        """未预告天气的规则最坏包络；已知本窗精确起点后不重复悲观。"""
        weather = state.weather or {}
        known_starts = []
        for w in (weather.get("active") or []) + (weather.get("forecast") or []):
            start = w.get("startRound") or w.get("start")
            if start is not None:
                known_starts.append(int(start))
        for lo, hi in UNKNOWN_WEATHER_WINDOWS:
            if lo <= abs_round <= hi + 59:
                return not any(lo <= start <= hi for start in known_starts)
        return False

    def _weather_tax_at(self, state, route_type, abs_round, conservative=True,
                        worst_unknown_weather=False):
        """按公开天气计算某一帧通行倍率。

        我方账本使用当前/预告天气；对手判死下界可传 conservative=False，
        等价于给对手晴天极速，避免误判它到不了。
        """
        if not conservative:
            return 1000
        weather_type = self._weather_type_at(state, abs_round)
        tax = P.WEATHER_MOVE_TAX.get((weather_type, route_type))
        if tax:
            return tax
        if worst_unknown_weather and weather_type is None \
                and self._unknown_weather_possible(state, abs_round):
            if route_type == P.WATER:
                return P.WEATHER_MOVE_TAX[(P.HEAVY_RAIN, P.WATER)]
            if route_type == P.MOUNTAIN:
                return P.WEATHER_MOVE_TAX[(P.MOUNTAIN_FOG, P.MOUNTAIN)]
        return 1000

    def _edge_dynamic_frames(self, state, edge, elapsed, boost_type, boost_rem,
                             conservative_weather=True,
                             worst_unknown_weather=False):
        """逐帧推进一条边，真实消耗有限时长的马/疾行。"""
        need = state.graph.edge_total_move(edge)
        moved = 0
        frames = 0
        route_type = edge.get("routeType")
        while moved < need and frames < 1000:
            speed = self._boost_speed(boost_type) if boost_rem > 0 \
                else P.BASE_SPEED
            tax = self._weather_tax_at(
                state, route_type, state.round + elapsed + frames,
                conservative=conservative_weather,
                worst_unknown_weather=worst_unknown_weather)
            moved += max(1, int(speed * 1000 / tax))
            frames += 1
            if boost_rem > 0:
                boost_rem -= 1
                if boost_rem <= 0:
                    boost_type = None
        return frames, boost_type, boost_rem

    def _travel_dynamic(self, state, src, dst, boost_type=None, boost_rem=0,
                        start_elapsed=0, include_current_process=False,
                        include_intermediate_process=True,
                        conservative_weather=True, return_boost=False,
                        worst_unknown_weather=False, blocked_nodes=None):
        """规则口径 ETA：路线距离/类型 + 公开天气 + 有限马/疾行时长。

        Dijkstra 状态携带 boost_rem，确保疾行令 15 帧、快马 20 帧、短马
        14 帧不会被错误套到整段终局路线。
        """
        import heapq
        if not src or not dst:
            if return_boost:
                return 999, [], None, 0
            return 999, []
        init_type, init_rem = boost_type, boost_rem
        elapsed0 = int(start_elapsed)
        if include_current_process:
            proc = self._node_process_frames_at(
                state, src, state.round + elapsed0,
                worst_unknown_weather=worst_unknown_weather)
            elapsed0 += proc
            init_type, init_rem = self._consume_boost(init_type, init_rem, proc)
        if src == dst:
            if return_boost:
                return elapsed0, [src], init_type, init_rem
            return elapsed0, [src]
        start_state = (src, init_type or "", int(init_rem))
        dist = {start_state: elapsed0}
        prev = {}
        pq = [(elapsed0, start_state)]
        best_state = None
        while pq:
            elapsed, cur_state = heapq.heappop(pq)
            if elapsed > dist.get(cur_state, 999):
                continue
            node_id, btype, brem = cur_state
            btype = btype or None
            if node_id == dst:
                best_state = cur_state
                break
            for nb, edge in state.graph.neighbors(node_id):
                if blocked_nodes and nb in blocked_nodes:
                    continue
                ef, nb_type, nb_rem = self._edge_dynamic_frames(
                    state, edge, elapsed, btype, brem,
                    conservative_weather=conservative_weather,
                    worst_unknown_weather=worst_unknown_weather)
                nd = elapsed + ef
                if include_intermediate_process and nb != dst:
                    proc = self._node_process_frames_at(
                        state, nb, state.round + nd,
                        worst_unknown_weather=worst_unknown_weather)
                    if proc:
                        nd += proc
                        nb_type, nb_rem = self._consume_boost(
                            nb_type, nb_rem, proc)
                nxt_state = (nb, nb_type or "", int(nb_rem))
                if nd < dist.get(nxt_state, 999):
                    dist[nxt_state] = nd
                    prev[nxt_state] = cur_state
                    heapq.heappush(pq, (nd, nxt_state))
        if best_state is None:
            if return_boost:
                return 999, [], None, 0
            return 999, []
        path = [best_state[0]]
        cur_state = best_state
        while cur_state != start_state:
            cur_state = prev[cur_state]
            path.append(cur_state[0])
        path.reverse()
        _, final_type, final_rem = best_state
        if return_boost:
            return dist[best_state], path, final_type or None, final_rem
        return dist[best_state], path

    def _delivery_need(self, state, cur, speed, move_factor=1.0,
                       include_current_process=False):
        """从 cur 到可交付的真实剩余帧：边长 + 处理中转 + 验核 + 交付。

        move_factor 只作用在移动帧上；处理/验核读条不因对手速度余量打折。
        """
        to_gate, p1 = self._shortest(state, cur, state.gate_node, speed)
        gate_term, p2 = self._shortest(
            state, state.gate_node, state.terminal_node, speed)
        if not p1 or not p2:
            return 999
        proc = self._path_process_frames(
            state, p1, include_current=include_current_process)
        proc += self._path_process_frames(state, p2)
        return (to_gate + gate_term) * move_factor + proc \
            + self._gate_verify_frames(state) + DELIVER_FRAMES

    def _my_need(self, state, cur):
        include_current = False
        if cur != state.gate_node:
            node = state.node(cur)
            include_current = bool(node.get("processType")
                                   and node.get("processType") != "VERIFY"
                                   and (node.get("processRound") or 0) > 0
                                   and not self._node_processed(state, cur))
        boost_type, boost_rem, _ = self._active_speed_buff(state)
        start_cost = 0
        if not boost_type and self._can_rush_speed(state):
            # 疾行令占主车队动作；本帧不能同时 MOVE，但下一帧起有 15 帧增益。
            boost_type, boost_rem = P.RUSH_SPEED, RUSH_SPEED_FRAMES
            start_cost = 1
        to_gate, _, boost_type, boost_rem = self._travel_dynamic(
            state, cur, state.gate_node, boost_type, boost_rem,
            start_elapsed=start_cost,
            include_current_process=include_current,
            conservative_weather=True, return_boost=True)
        if to_gate >= 999:
            return 999
        verify = self._gate_verify_frames(state)
        boost_type, boost_rem = self._consume_boost(
            boost_type, boost_rem, verify)
        gate_term, _ = self._travel_dynamic(
            state, state.gate_node, state.terminal_node,
            boost_type, boost_rem, start_elapsed=to_gate + verify,
            conservative_weather=True)
        if gate_term >= 999:
            return 999
        return gate_term + DELIVER_FRAMES

    def _beats_opp_to_task(self, state, task, my_eta, proc, opp_pos):
        return self._farm_task_race_factor(
            state, task["nodeId"], my_eta, proc, opp_pos) >= 1.0

    def _should_leave(self, state, cur):
        rnd = state.round
        remain = state.duration_round - rnd
        # ⓪ 对手已交付/退赛：墙没有对象了，立即动身（实战 r412 对手交付
        #    后还站到 r475 的 63 帧站岗白丢 3 鲜度）
        opp = state.opp
        if not opp or opp.get("delivered") or opp.get("retired"):
            return True
        # ⓪b 当前 S10 墙快风化且可以证明 S14 接墙后对手到不了终点：
        # 才提前转宫门。不能只看防守值低，否则会把"守到数学死"
        # 误改成无谓早走。
        if self._handoff_kills(state, cur):
            self.camp_node = state.gate_node
            return True
        # ① 死线余量：只看我方最迟出发帧。对手静止多久不是出发条件；
        #    warden 的目标是焊到最后一刻，不能被"它等了 100 帧"吓走。
        if remain <= self._my_need(state, cur) + self.EXIT_PAD:
            return True
        # ② 数学判死：只能用对手最快下界判死。这里故意不计处理中转/验核，
        # 也按疾行速度打折；若这个理想下界都超时，才算真的死。
        pos = opp.get("nextNodeId") or opp.get("currentNodeId")
        if pos:
            opp_need = self._opp_delivery_lower_bound(state, pos)
            if opp_need < 999 and opp_need > remain + self.OPP_DEAD_BUFFER:
                if self.log:
                    self.log.info(
                        "warden: opp mathematically dead "
                        "(lower-bound need %.0f > remain %d), leaving",
                        opp_need, remain)
                return True
        return False

    def _opp_delivery_lower_bound(self, state, cur):
        """对手可交付时间下界：只用于判死，必须极度保守。

        不加中途处理站/宫门验核/起手急策帧；给对手完整 15 帧疾行，
        并忽略天气减速。只有这份极限账本都来不及时，才算真的死。
        """
        to_gate, p1, boost_type, boost_rem = self._travel_dynamic(
            state, cur, state.gate_node, P.RUSH_SPEED, RUSH_SPEED_FRAMES,
            include_intermediate_process=False,
            conservative_weather=False, return_boost=True)
        gate_term, p2 = self._travel_dynamic(
            state, state.gate_node, state.terminal_node,
            boost_type, boost_rem, start_elapsed=to_gate,
            include_intermediate_process=False,
            conservative_weather=False)
        if not p1 or not p2:
            return 999
        return max(0, gate_term * self.OPP_SPEED_MARGIN) + DELIVER_FRAMES

    def _eta_to_gate(self, state, cur, speed, include_current=False):
        frames, path = self._shortest(state, cur, state.gate_node, speed)
        if not path:
            return float("inf")
        return frames + self._path_process_frames(
            state, path, include_current=include_current)

    def _handoff_kills(self, state, cur):
        """S10 -> S14 接墙必须可证明，而不是见低防就跑。"""
        return self._handoff_plan(state, cur) is not None

    def _handoff_farm_action(self, state, cur):
        plan = self._handoff_plan(state, cur)
        if not plan:
            return None
        res = state.me.get("resources") or {}
        has_horse = any(res.get(h, 0) > 0
                        for h in (P.FAST_HORSE, P.SHORT_HORSE))
        for t in state.claimable_tasks():
            if t.get("nodeId") != cur:
                continue
            if self._farm_task_blocked(state, t, has_horse):
                continue
            proc = t.get("processRound", 4) or 4
            if proc + 2 <= plan["slack"] \
                    and self._departure_slack(state, cur) >= proc + 2:
                return P.a_claim_task(t["taskId"])
        return None

    def _departure_slack(self, state, cur):
        return state.duration_round - state.round \
            - self._my_need(state, cur) - self.EXIT_PAD

    def _farm_here_safe(self, state, cur):
        action = self._farm_here(state, cur)
        if not action:
            return None
        cost = 0
        if action.get("action") == "CLAIM_TASK":
            tid = action.get("taskId")
            task = next((t for t in state.claimable_tasks()
                         if t.get("taskId") == tid), None)
            cost = (task.get("processRound", 4) if task else 4) + 2
        elif action.get("action") == "CLAIM_RESOURCE":
            cost = 2
        if cost and self._departure_slack(state, cur) < cost:
            return None
        return action

    def _handoff_plan(self, state, cur):
        """证明第一墙后转 S14 能接死。

        两类场景才考虑提前走：低防快风化，或对手已经贴近 S10 到
        不足以再落第二张 S10 卡。后者是实战里"等风化结束才发现
        补卡来不及"的根因。
        """
        if cur == state.gate_node or not self._opp_inbound(state, cur):
            return None
        defense = self._my_guard_defense(state, cur)
        if defense <= 0:
            return None
        edge_remain = self._opp_edge_remain_to(state, cur)
        if defense > self.HANDOFF_GUARD_DEFENSE \
                and edge_remain >= self.GUARD_MIN_LEAD:
            return None
        remain = state.duration_round - state.round
        if self._my_need(state, cur) + self.EXIT_PAD >= remain:
            return None

        boost_type, boost_rem, _ = self._active_speed_buff(state)
        start_cost = 0
        if not boost_type and self._can_rush_speed(state):
            boost_type, boost_rem = P.RUSH_SPEED, RUSH_SPEED_FRAMES
            start_cost = 1
        my_gate_eta, path = self._travel_dynamic(
            state, cur, state.gate_node, boost_type, boost_rem,
            start_elapsed=start_cost, conservative_weather=True)
        if not path:
            return None

        # 对手被当前墙挡到风化，再按最乐观规则冲宫门：
        # 不计天气/中转处理，只要这个下界仍能接死才允许换墙。
        opp_gate_eta = max(edge_remain, self._my_guard_remaining(state, cur))
        opp_to_gate, path = self._travel_dynamic(
            state, cur, state.gate_node, P.RUSH_SPEED, RUSH_SPEED_FRAMES,
            include_intermediate_process=False, conservative_weather=False)
        if not path:
            return None
        opp_gate_eta += opp_to_gate

        slack = opp_gate_eta - my_gate_eta - self.GUARD_MIN_LEAD
        if slack < 0:
            return None
        if slack > self.HANDOFF_EXIT_PAD:
            return None

        gate_wall = self._gate_wall_hold_lower_bound(state)
        after_gate = self._opp_delivery_lower_bound(state, state.gate_node)
        if after_gate >= 999:
            return None
        opp_after_wall_delivery = opp_gate_eta + gate_wall + after_gate
        if opp_after_wall_delivery <= remain:
            return None
        return {"slack": slack, "my_gate_eta": my_gate_eta,
                "opp_gate_eta": opp_gate_eta}

    def _gate_wall_hold_lower_bound(self, state):
        """S14 墙能拖住对手多久的保守下界。

        对手有足够果子或非 RUSH 期能用小分队补足破防时，不能把宫门卡
        当作 120 帧自然风化墙；接墙证明必须按它会被快速打穿处理。
        """
        opp = state.opp or {}
        good = opp.get("goodFruit")
        bad = opp.get("badFruit")
        if good is None or bad is None:
            return 0
        attack = min(2, max(0, good)) * 2 + min(2, max(0, bad)) * 3
        if attack >= self.GATE_GUARD_DEFENSE:
            return 0
        squad = opp.get("squadAvailable")
        if state.phase != P.PHASE_RUSH and squad is not None \
                and squad >= self.SQUAD_WEAKEN_COST \
                and attack + 2 >= self.GATE_GUARD_DEFENSE:
            return 0
        return self.GATE_GUARD_DEFENSE * self.GUARD_DECAY_FRAMES

    def _can_rush_speed(self, state):
        me = state.me
        return (state.phase == P.PHASE_RUSH
                and not state.has_move_buff()
                and (me.get("rushTacticUsedCount") or 0) == 0
                and not self._rush_tactic_tried
                and me.get("goodFruit", 0) >= 3)

    def _rush_speed_action(self, state):
        if not self._can_rush_speed(state):
            return None
        self._rush_tactic_tried = True
        return P.a_rush_speed()

    def _opp_edge_remain_to(self, state, node_id):
        opp = state.opp
        if not (opp and opp.get("routeEdgeId")
                and opp.get("nextNodeId") == node_id):
            return 0.0
        total = opp.get("edgeTotalMs") or 0
        done = opp.get("edgeProgressMs") or 0
        return max(0, total - done) / 1000.0

    def _my_guard_remaining(self, state, node_id):
        g = state.node(node_id).get("guard") or {}
        defense = self._my_guard_defense(state, node_id)
        if defense <= 0:
            return 0
        age = None
        for key in ("ageRound", "age", "ageFrames"):
            if g.get(key) is not None:
                try:
                    age = int(g[key])
                    break
                except (TypeError, ValueError):
                    pass
        if age is None:
            for key in ("completeRound", "completionRound", "finishRound",
                        "completedRound", "round"):
                if g.get(key) is not None:
                    try:
                        age = max(0, state.round - int(g[key]))
                        break
                    except (TypeError, ValueError):
                        pass
        if age is not None:
            rem_to_next = self.GUARD_DECAY_FRAMES \
                - (age % self.GUARD_DECAY_FRAMES)
            return rem_to_next + (defense - 1) * self.GUARD_DECAY_FRAMES
        # 无年龄字段时用保守下界，避免高估第一墙能拖住的时间。
        return 1 + (defense - 1) * self.GUARD_DECAY_FRAMES

    # ================= 小分队 =================

    def _squad_avail(self, state):
        v = state.me.get("squadAvailable")
        if v is not None:
            return v
        return max(0, 8 - self._squad_spent)

    def squad_action(self, state):
        if state.phase == P.PHASE_RUSH:
            return None
        avail = self._squad_avail(state)
        if avail <= 0:
            return None
        me = state.me
        rnd = state.round

        # 1) 被冻在边上：削弱自救（守望者也可能被对手狙击）
        nxt = me.get("nextNodeId")
        if avail >= self.SQUAD_WEAKEN_COST \
                and me.get("routeEdgeId") and nxt and state.enemy_guard(nxt) \
                and rnd - self._weaken_sent.get(nxt, -999) \
                >= self.WEAKEN_RESEND_GAP:
            self._weaken_sent[nxt] = rnd
            self._squad_spent += self.SQUAD_WEAKEN_COST
            return P.a_squad_weaken(nxt)

        # S02 长锁收尾：只提前标记本站，不预投互斥分叉。回放 093732
        # 里转农后才派 S02 标记，落地虽赶上但仍多等一拍；临界鲜度时
        # 提前把本站 4 帧处理压到 2 帧，锁结束后每帧都能抢。
        if self._s02_finish_scout_due(state) \
                and self._can_spend_squad(state, self.SQUAD_SCOUT_COST,
                                          purpose="farm_scout") \
                and not self._has_our_mark(state, "S02") \
                and rnd - self._scout_sent.get("S02", -999) >= 20:
            self._scout_sent["S02"] = rnd
            self._squad_spent += self.SQUAD_SCOUT_COST
            return P.a_squad_scout("S02")

        # 主车队还卡在 S02 镜像锁/处理站争用时，探路标记和预清障很容易
        # 在离站前过期；人手留给后段削弱、续防和真正临近的处理站标记。
        if self._defer_nonurgent_squad(state):
            return None

        stage = self._squad_stage(state)

        # 2) S10/S14 竞速占位期：小分队的第一职责是买速度。
        # 这里不保 4 人墙战底仓，因为没抢到关隘所有权，后面的墙根本立不住。
        if stage == "rush":
            speed = self._squad_speed_action(state, reserve=False)
            if speed:
                return speed

        # 3) 守墙期：续防是墙的一部分，优先级高于探路和清障。
        reinforce = self._squad_reinforce_action(state)
        if reinforce:
            return reinforce

        # 4) 农任务终局/S02 僵持后转农：人手全转任务点标记（处理帧 -3）
        target = getattr(self, "_farm_target", None)
        if target and self._can_spend_squad(state, self.SQUAD_SCOUT_COST,
                                            purpose="farm_scout") \
                and not self._has_our_mark(state, target) \
                and rnd - self._scout_sent.get(target, -999) >= 20 \
                and self._my_eta(state, target) <= self.SCOUT_DISPATCH_ETA:
            self._scout_sent[target] = rnd
            self._squad_spent += self.SQUAD_SCOUT_COST
            return P.a_squad_scout(target)

        # 5) 其他阶段只在不买穿墙战底仓时买速度。
        speed = self._squad_speed_action(state, reserve=True)
        if speed:
            return speed
        return None

    def _squad_speed_action(self, state, reserve=True):
        me = state.me
        rnd = state.round
        clear_purpose = "clear" if reserve else "rush_clear"
        scout_purpose = "route_scout" if reserve else "rush_scout"

        # 路上有障碍先清：这是最硬的提速，也是拆掉对手白菜价强通把手。
        if self._can_spend_squad(state, self.SQUAD_CLEAR_COST,
                                 purpose=clear_purpose):
            for nid in self._clear_plan:
                if not state.has_obstacle(nid):
                    continue
                if rnd - self._clear_sent.get(nid, -999) < 20:
                    continue
                self._clear_sent[nid] = rnd
                self._squad_spent += self.SQUAD_CLEAR_COST
                return P.a_squad_clear(nid)

        # 处理站探路标记：真实 ETA（含边上剩余进度）进入寿命窗口
        # （≤38）才派——落地早于我们进站、45 帧寿命盖住到站。
        cur_n = me.get("currentNodeId")
        locked = False
        if cur_n and not me.get("routeEdgeId"):
            nd = state.node(cur_n)
            opp = state.opp
            locked = (nd.get("processType")
                      and (nd.get("processRound") or 0) > 0
                      and not self._node_processed(state, cur_n)
                      and bool(opp and opp.get("currentNodeId") == cur_n))
        if self._can_spend_squad(state, self.SQUAD_SCOUT_COST,
                                 purpose=scout_purpose) and not locked \
                and me.get("currentNodeId") != self.camp_node:
            for nid in self._scout_plan:
                if self._scout_sent.get(nid):
                    continue
                if self._has_our_mark(state, nid):
                    self._scout_sent[nid] = rnd
                    continue
                eta = self._my_eta(state, nid)
                if eta > self.SCOUT_DISPATCH_ETA:
                    continue
                self._scout_sent[nid] = rnd
                self._squad_spent += self.SQUAD_SCOUT_COST
                return P.a_squad_scout(nid)
        return None

    def _s02_finish_scout_due(self, state):
        me = state.me
        if me.get("routeEdgeId") or me.get("currentNodeId") != "S02":
            return False
        if self._node_processed(state, "S02") or self._s02_won_window:
            return False
        node = state.node("S02")
        if not (node.get("processType")
                and node.get("processType") != "VERIFY"
                and (node.get("processRound") or 0) > 0):
            return False
        return self._score_farm_mode or me.get("freshness", 100.0) <= 81.7

    def _squad_reinforce_action(self, state):
        if not self._can_spend_squad(state, self.SQUAD_REINFORCE_COST,
                                     purpose="reinforce"):
            return None
        me = state.me
        rnd = state.round
        camp = self.camp_node
        if me.get("currentNodeId") == camp:
            g = state.node(camp).get("guard")
            if g and g.get("ownerTeamId") == state.my_team \
                    and g.get("active", g.get("defense", 0) > 0):
                defense = g.get("defense", 0) or 0
                cap = g.get("maxDefense") or 7
                if defense + 2 <= cap and self._opp_inbound(state, camp) \
                        and rnd - self._reinforce_sent >= 20:
                    self._reinforce_sent = rnd
                    self._squad_spent += self.SQUAD_REINFORCE_COST
                    return P.a_squad_reinforce(camp)
        return None

    def _can_spend_squad(self, state, cost, purpose="misc"):
        avail = self._squad_avail(state)
        if avail < cost:
            return False
        if purpose in ("weaken", "reinforce", "farm_scout",
                       "rush_clear", "rush_scout"):
            return True
        me = state.me
        opp = state.opp
        guard_threat = (not self._score_farm_mode
                        and not me.get("verified")
                        and opp and not opp.get("delivered")
                        and not opp.get("retired"))
        floor = self.SQUAD_NONURGENT_RESERVE if guard_threat else 0
        return avail - cost >= floor

    def _squad_stage(self, state):
        if self._score_farm_mode:
            return "farm"
        me = state.me
        cur = me.get("currentNodeId") or me.get("nextNodeId")
        if cur and cur == self.camp_node:
            return "wall"
        if cur and cur == state.gate_node:
            return "wall"
        return "rush"

    def _defer_nonurgent_squad(self, state):
        me = state.me
        if me.get("routeEdgeId"):
            return False
        cur = me.get("currentNodeId")
        if not cur:
            return False
        if self._score_farm_mode:
            return False
        # S02 的战略是 RUSH 前锁住对手；未赢窗/未换乘时离站时间不可控，
        # 此时派出的 S04/S05/S10 标记大概率白白过期。
        if cur == "S02" and not self._node_processed(state, cur) \
                and not self._s02_won_window:
            return True
        if cur == "S02":
            return False
        nd = state.node(cur)
        opp = state.opp
        return (bool(nd.get("processType"))
                and (nd.get("processRound") or 0) > 0
                and not self._node_processed(state, cur)
                and bool(opp and opp.get("currentNodeId") == cur))

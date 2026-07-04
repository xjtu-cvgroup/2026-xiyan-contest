"""S10 守望者策略（W1）——走廊封锁流的完整工程实现。

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

W1 刻意不做的：鲜度管理（无冰走廊，砖比漆重要）、悬赏、任务巡回、
对镜像守望者的对策（双 camp 局自然互不干扰各自交付）。
"""
from . import protocol as P
from .strategy import BaselineStrategy

GATE_VERIFY_FRAMES = 6
DELIVER_FRAMES = 2
STATION_PAD = 10           # 最短路不含途中固定处理站读条，补齐


class WardenStrategy(BaselineStrategy):
    """S10 守望者：竞速占关 → 虚卡封锁 → 判死离场。"""

    GUARD_EXTRA = 2            # 反应卡额外投入（关键关隘 → 防 6）
    GUARD_RETRY_GAP = 25       # 同节点补卡最小间隔（防拒绝风暴）
    FRUIT_RESERVE = 5          # 好果底仓：交付要求 >0，窗口牌还要嚼几篓
    EXIT_PAD = 25              # 离场安全余量（帧）
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

    def __init__(self, logger=None):
        super().__init__(logger)
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
        self._score_farm_mode = False  # S02 锁死/RUSH 后只抢任务分，不再奔终点

    # ================= 初始化 =================

    def on_start(self, state):
        # 平台 start 消息里位置/障碍数据可能不全（实战 0/8 小队实锤：
        # 计划在这里建成空表且永不重建）——只做日志，计划延迟到首帧
        # inquire（state 完整）再建，见 decide() 的 _build_plans
        if self.log:
            self.log.info("warden: on_start (plans deferred to first frame)")

    def _build_plans(self, state):
        self.camp_node = self._pick_camp(state)
        me_pos = state.me.get("currentNodeId")
        path = []
        if me_pos:
            _, path = state.graph.shortest_path(
                me_pos, self.camp_node, P.BASE_SPEED)
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
        me_pos = state.me.get("currentNodeId")
        path = []
        if me_pos:
            _, path = state.graph.shortest_path(
                me_pos, state.gate_node, P.BASE_SPEED)
            path = path or []
        for nid in path:
            if state.node(nid).get("nodeType") == "KEY_PASS":
                return nid
        for nid, node in state.nodes.items():
            if node.get("nodeType") == "KEY_PASS":
                return nid
        return "S10"

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

    def _maybe_fallback_gate(self, state):
        """当前墙被买穿/正在被攻坚/已失去必经性 → 转场宫门 S14 重筑墙。

        S10 是第一道墙，不是最后一块地。对手一旦把墙转成强通税或攻坚
        读条，继续守原点的收益会快速归零；原先套路就是果断放弃，到
        下一个唯一汇合点继续埋伏。
        """
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
            _, pth = state.graph.shortest_path(pos, gate, P.BASE_SPEED)
            breached = bool(pth) and camp not in pth   # 它去宫门已不经过关隘
        if forcing:
            self._last_inbound = state.round   # 转运=正在逼近，别当埋伏流
        if forcing or breaking or breached:
            if self.log:
                self.log.info("warden: fallback to gate wall (%s)",
                              "forced-pass" if forcing else
                              ("break-guard" if breaking else "breached"))
            self.camp_node = gate

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
        # 锁死，未交付世界里好果一文不值，烧果免费
        if contest.get("contestType") in (P.CONTEST_DOCK, P.CONTEST_TASK) \
                and me.get("goodFruit", 0) > 1:
            return P.CARD_XIAN_GONG
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
            if not state.has_move_buff():
                for h in (P.FAST_HORSE, P.SHORT_HORSE):
                    if res.get(h, 0) > 0:
                        return P.a_use_resource(h)
            return None

        cur = me.get("currentNodeId")
        gate, terminal = state.gate_node, state.terminal_node
        remain = state.duration_round - state.round

        # ---- 墙优先级：对手已经踏边时，本帧能设卡就先设卡 ----
        # 任务是墙成立后的白捡收益；不能反过来让 3-4 帧读条吃掉落卡窗口。
        if cur in (self.camp_node, gate):
            guard = self._reactive_guard(state, cur)
            if guard:
                return guard

        # ---- 农任务终局（S02 镜像锁死等场景）----
        # 我方交付已不可能：分数只剩未交付任务分可挣。但对手还活着时
        # 必须继续争（让行=放它出去交付）；对手也死了才让行转农
        if self._score_farm_mode and not me.get("verified"):
            return self._farm_endgame(state, cur)
        if self._s02_lock_hold(state, cur):
            node = state.node(cur)
            needs = (node.get("processType")
                     and node.get("processType") != "VERIFY"
                     and node.get("processRound", 0) > 0)
            if needs and not self._processed_here:
                return P.a_process()
            return P.a_wait()

        farm_deadline = (not me.get("verified")
                         and remain < self._my_need(state, cur) - 5)
        if farm_deadline:
            if self._opp_alive_can_deliver(state, remain):
                # 能守才守：我在关隘/宫门且它去宫门仍必经此处 → 驻守拖死
                # 它仍有价值；否则（僵持散场/它已越关）我死=立刻转农
                opp = state.opp
                pos = opp and (opp.get("nextNodeId")
                               or opp.get("currentNodeId"))
                wallable = False
                if pos and cur in (self.camp_node, state.gate_node) \
                        and cur != pos:
                    _, pth = state.graph.shortest_path(
                        pos, state.gate_node, P.BASE_SPEED)
                    wallable = bool(pth) and cur in pth
                if not wallable:
                    self._score_farm_mode = True
                    return self._farm_endgame(state, cur)
            else:
                # 用户 spec：双死判定成立的那一帧立刻转最优任务，
                # 不为 9 帧先手多等（早动 = 抢刷新波占位 + 抢短马快马）
                self._score_farm_mode = True
                return self._farm_endgame(state, cur)

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
                task = self._farm_here(state, cur)
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
                task = self._farm_here(state, cur)
                if task:
                    return task
            if state.phase == P.PHASE_RUSH:
                return P.a_verify_gate()
            return P.a_wait()

        # ---- 固定处理站（途中驿站/码头/水驿必须处理完才能走）----
        node = state.node(cur)
        needs = (node.get("processType") and node.get("processType") != "VERIFY"
                 and node.get("processRound", 0) > 0)
        if needs and not self._processed_here:
            proc = (state.opp.get("currentProcess") or {})
            if proc.get("targetNodeId") == cur:
                return P.a_wait()            # 排队，别开无谓的码头窗口
            return P.a_process()

        leaving = self._should_leave(state, cur)

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
                    oeta, op = state.graph.shortest_path(
                        pos, cur, P.BASE_SPEED)
                    near = bool(op) and oeta <= 12   # 读条4+设卡5+余量
            if (not near) or self._my_active_guard(state, cur):
                task = self._farm_here(state, cur)
                if task:
                    return task
            return P.a_wait()

        # ---- 收官段 ----
        if cur == camp:
            guard = self._depart_guard(state, cur)
            if guard:
                return guard
        return self._advance(state, cur, gate)

    def _advance(self, state, cur, target):
        """朝 target 走一步：障碍优先强通（税 8 免冻），敌卡一击可破
        则攻坚、否则强通——守望者永不等待。"""
        me = state.me
        nxt = state.graph.next_hop(cur, target, state.my_speed())
        if nxt is None:
            return P.a_wait()
        g = state.enemy_guard(nxt)
        if g:
            good, bad = me.get("goodFruit", 0), me.get("badFruit", 0) or 0
            gf = min(2, max(0, good - self.FRUIT_RESERVE))
            bf = min(2, bad)
            if gf * 2 + bf * 3 >= (g.get("defense", 0) or 0):
                return P.a_break_guard(nxt, gf, bf)
            return P.a_forced_pass(nxt)
        if state.has_obstacle(nxt):
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
        if state.me.get("verified") or self._processed_here:
            return False
        opp = state.opp
        return bool(opp and not opp.get("delivered") and not opp.get("retired")
                    and not opp.get("routeEdgeId")
                    and opp.get("currentNodeId") == cur)

    # ---- 封锁 ----

    @staticmethod
    def _my_eta(state, node_id):
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
        f, path = state.graph.shortest_path(pos, node_id)
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

    def _guardable(self, state, node_id):
        """该节点当前无任何有效卡且我方成本可负担。"""
        g = state.node(node_id).get("guard")
        if g and g.get("ownerTeamId") \
                and g.get("active", g.get("defense", 0) > 0):
            return False
        extra = 1 if node_id == state.gate_node else self.GUARD_EXTRA
        cost = 1 + extra                   # 关键关隘/宫门底价 1
        if state.me.get("goodFruit", 0) - cost < self.FRUIT_RESERVE:
            return False
        return state.round - self._guard_sent.get(node_id, -999) \
            >= self.GUARD_RETRY_GAP

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
        eta, path = state.graph.shortest_path(pos, node_id, P.BASE_SPEED)
        return bool(path) and eta <= eta_cap

    GUARD_MIN_LEAD = 5         # 提交T→T+4完成→T+5起拦：剩≥5帧即可拦（实战5000ms被6误放的修正）

    def _reactive_guard(self, state, cur):
        """虚卡纪律：对手上边才落卡；卡亡且它仍在边上 → 立即补。

        来得及才立（实战 r256 教训：对手 2 帧后进站，读条 4 帧的卡
        r260 才成型，白烧 3 好果拦了个寂寞）。"""
        if not self._opp_inbound(state, cur):
            return None
        opp = state.opp
        total = opp.get("edgeTotalMs") or 0
        done = opp.get("edgeProgressMs") or 0
        if total and (total - done) / 1000.0 < self.GUARD_MIN_LEAD:
            return None
        if not self._guardable(state, cur):
            return None
        self._guard_sent[cur] = state.round
        extra = 1 if cur == state.gate_node else self.GUARD_EXTRA
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
        if not self._guardable(state, cur):
            return None
        self._guard_sent[cur] = state.round
        extra = 1 if cur == state.gate_node else self.GUARD_EXTRA
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
            tpl = t.get("taskTemplateId")
            if tpl == "T04" or (tpl == "T06" and not has_horse):
                continue
            return P.a_claim_task(t["taskId"])
        return self._claim_en_route(state, cur)

    # ---- 农任务终局 ----

    def _at_contested_station(self, state, cur):
        """还站在与对手争夺中的未处理站上（懦夫博弈仍在进行）。"""
        node = state.node(cur)
        opp = state.opp
        return bool(node.get("processType")
                    and (node.get("processRound") or 0) > 0
                    and not self._processed_here
                    and opp and opp.get("currentNodeId") == cur)

    def _opp_alive_can_deliver(self, state, remain):
        """还需要用争夺/驻守拖住对手吗？对手已交付/退赛/数学死=不需要。"""
        opp = state.opp
        if not opp or opp.get("delivered") or opp.get("retired"):
            return False
        pos = opp.get("nextNodeId") or opp.get("currentNodeId")
        if not pos:
            return True
        to_gate, p1 = state.graph.shortest_path(
            pos, state.gate_node, P.BASE_SPEED)
        gate_term, p2 = state.graph.shortest_path(
            state.gate_node, state.terminal_node, P.BASE_SPEED)
        if not p1 or not p2:
            return False
        need = (to_gate + gate_term) * self.OPP_SPEED_MARGIN \
            + GATE_VERIFY_FRAMES + DELIVER_FRAMES
        return need <= remain      # 零缓冲：它数学死即死，秒让抢农起跑

    def _farm_endgame(self, state, cur):
        """交付双死后的任务分收割：让行出站 → 追最近可达任务 →
        小分队全转探路标记提速领取。"""
        me = state.me
        node = state.node(cur)
        needs = (node.get("processType") and node.get("processType") != "VERIFY"
                 and node.get("processRound", 0) > 0)
        if needs and not self._processed_here:
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
            tpl = t.get("taskTemplateId")
            if tpl == "T04" or (tpl == "T06" and not has_horse):
                continue
            eta, path = state.graph.shortest_path(
                cur, t["nodeId"], state.my_speed())
            if not path:
                continue
            if self._farm_backtrack_step(state, cur, path):
                continue
            proc = t.get("processRound", 4) or 4
            expire = t.get("expireRound") or 0
            if expire and state.round + eta + proc + 2 > expire:
                continue
            if state.round + eta + proc > state.duration_round:
                continue
            if opp_pos:
                oeta, opath = state.graph.shortest_path(
                    opp_pos, t["nodeId"], P.BASE_SPEED)
                if opath and oeta <= eta:
                    continue          # 它更近/同距：别跟屁股，换线抢别的桶
            back = self._farm_backtrack_step(state, cur, path)
            rank = (1 if back else 0, -int(t.get("score", 0) or 0), eta)
            if best_rank is None or rank < best_rank:
                best, best_rank = t["nodeId"], rank
        if best is None:
            # 无可追实例 → 驻守刷新候选点吃下一波。分桶原则：先手 4 帧
            # 只在争同一实例时值钱——只选"我比对手近"的点，它先出发
            # 的先手作用于我们不去的桶，赤字归零；全被占回退任意最近
            cands = set()
            for nodes in (state.task_candidates or {}).values():
                cands.update(nodes)
            fb, fb_rank = None, None
            for nid in cands:
                if nid == cur and len(cands) > 1:
                    continue
                eta, path = state.graph.shortest_path(
                    cur, nid, state.my_speed())
                if not path:
                    continue
                if state.round + eta > state.duration_round:
                    continue
                rank = (1 if self._farm_backtrack_step(state, cur, path)
                        else 0, eta)
                if fb_rank is None or rank < fb_rank:
                    fb, fb_rank = nid, rank
                if opp_pos:
                    oeta, opath = state.graph.shortest_path(
                        opp_pos, nid, P.BASE_SPEED)
                    if opath and oeta <= eta:
                        continue          # 它更近：让给它，别追尾
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

    # ---- 收官判定 ----

    def _my_need(self, state, cur):
        g = state.graph
        to_gate, p1 = g.shortest_path(cur, state.gate_node, state.my_speed())
        gate_term, p2 = g.shortest_path(state.gate_node, state.terminal_node,
                                        state.my_speed())
        if not p1 or not p2:
            return 999
        return (to_gate + GATE_VERIFY_FRAMES + gate_term + DELIVER_FRAMES
                + STATION_PAD)

    def _should_leave(self, state, cur):
        rnd = state.round
        remain = state.duration_round - rnd
        # ⓪ 对手已交付/退赛：墙没有对象了，立即动身（实战 r412 对手交付
        #    后还站到 r475 的 63 帧站岗白丢 3 鲜度）
        opp = state.opp
        if not opp or opp.get("delivered") or opp.get("retired"):
            return True
        # ① 死线余量：只看我方最迟出发帧。对手静止多久不是出发条件；
        #    warden 的目标是焊到最后一刻，不能被"它等了 100 帧"吓走。
        if remain <= self._my_need(state, cur) + self.EXIT_PAD:
            return True
        # ② 数学判死：对手全速（含骑马余量）也到不了终点
        if True:
            pos = opp.get("nextNodeId") or opp.get("currentNodeId")
            if pos:
                to_gate, p1 = state.graph.shortest_path(
                    pos, state.gate_node, P.BASE_SPEED)
                gate_term, p2 = state.graph.shortest_path(
                    state.gate_node, state.terminal_node, P.BASE_SPEED)
                if p1 and p2:
                    opp_need = (to_gate + gate_term) * self.OPP_SPEED_MARGIN \
                        + GATE_VERIFY_FRAMES + DELIVER_FRAMES
                    if opp_need > remain + self.OPP_DEAD_BUFFER:
                        if self.log:
                            self.log.info(
                                "warden: opp mathematically dead "
                                "(need %.0f > remain %d), leaving",
                                opp_need, remain)
                        return True
        return False

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
        if avail < 2:
            return None
        me = state.me
        rnd = state.round

        # 1) 被冻在边上：削弱自救（守望者也可能被对手狙击）
        nxt = me.get("nextNodeId")
        if me.get("routeEdgeId") and nxt and state.enemy_guard(nxt) \
                and rnd - self._weaken_sent.get(nxt, -999) \
                >= self.WEAKEN_RESEND_GAP:
            self._weaken_sent[nxt] = rnd
            self._squad_spent += 2
            return P.a_squad_weaken(nxt)

        # 2) 开局清障计划：沿途 + 关隘的障碍逐个远程清掉
        for nid in self._clear_plan:
            if not state.has_obstacle(nid):
                continue
            if rnd - self._clear_sent.get(nid, -999) < 20:
                continue
            self._clear_sent[nid] = rnd
            self._squad_spent += 2
            return P.a_squad_clear(nid)

        # 2.4) 农任务终局：人手全转任务点标记（处理帧 -3 提速领取）
        target = getattr(self, "_farm_target", None)
        if target and avail >= 1 and not self._has_our_mark(state, target) \
                and rnd - self._scout_sent.get(target, -999) >= 20 \
                and self._my_eta(state, target) <= self.SCOUT_DISPATCH_ETA:
            self._scout_sent[target] = rnd
            self._squad_spent += 1
            return P.a_squad_scout(target)

        # 2.5) 处理站探路标记：真实 ETA（含边上剩余进度）进入寿命窗口
        #      （≤38）才派——落地早于我们进站、45 帧寿命盖住到站。
        #      锁死前置（实战 vs2769：S02 互锁期间派的 3 个标记全过期
        #      白烧）：停在未处理完的处理站且对手同站=疑似锁死，不派
        cur_n = me.get("currentNodeId")
        locked = False
        if cur_n and not me.get("routeEdgeId"):
            nd = state.node(cur_n)
            opp = state.opp
            locked = (nd.get("processType")
                      and (nd.get("processRound") or 0) > 0
                      and not self._processed_here
                      and bool(opp and opp.get("currentNodeId") == cur_n))
        if avail >= 1 and not locked \
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
                self._squad_spent += 1
                return P.a_squad_scout(nid)

        # 3) 守墙增援：卡在挨打/风化且对手仍在边上 → 续防
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
                    self._squad_spent += 2
                    return P.a_squad_reinforce(camp)
        return None

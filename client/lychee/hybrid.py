"""隐藏地图混合策略：固定主墙 + 2621 移动守望者。

战略按地图拓扑切换控制方式：
- 当前地图存在真正必经的 KEY_PASS：完整沿用 Warden；
- KEY_PASS 可绕：移动守望者主动抢汇合点，Planner 负责可塞入余量的任务；
- 移动局后段能严格证明抢到宫门先手：粘性切入 S14 Warden。
"""
import heapq
import math

from . import protocol as P
from .planner import RUSH_EARLIEST
from .strategy import PlannerStrategy, Strategy
from .warden import DELIVER_FRAMES, RUSH_SPEED_FRAMES, WardenStrategy


class HybridStrategy(Strategy):
    MODE_PRIMARY = "PRIMARY_WARDEN"
    MODE_MOBILE = "MOBILE_WARDEN"
    MODE_SCORE = MODE_MOBILE       # 兼容既有测试/外部观测字段
    MODE_GATE = "GATE_WARDEN"

    # S14 设卡 T->T+4 完成、T+5 才拦。再留 3 帧 ETA 误差；后续任务
    # 只能消费这条余量之外的领先预算。
    GATE_LEAD_MARGIN = 8
    GATE_TASK_FLOOR = 120
    GATE_COMMIT_ROUND = RUSH_EARLIEST - 40
    GATE_MAX_ETA = 150
    GATE_THREAT_ETA = 120
    GATE_SHADOW_TRAIL_MAX = 40
    GATE_GOOD_FRUIT_FLOOR = 7  # 防4宫门卡 2 篓 + Warden 5 篓底仓
    MOBILE_APPROACH_MAX_DETOUR = 12
    MOBILE_ROUTE_TOLERANCE = 12
    S02_REENTRY_TOLERANCE = 8

    def __init__(self, logger=None):
        self.log = logger
        self.planner = PlannerStrategy(logger)
        self.warden = WardenStrategy(logger)
        self.mode = None
        self.primary_choke = None
        self.mobile_target = None
        self.mobile_plan = None
        self._mobile_hold_node = None
        self._gate_pace_active = False
        self._s02_farm_only = False
        self._s02_deny_only = False

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
                self.mode = self.MODE_MOBILE
            if self.log:
                self.log.info("hybrid: initial mode=%s primary=%s",
                              self.mode, self.primary_choke)

        recovery = self._process_required_recovery(state)
        if recovery:
            return recovery

        # S02 确实在竞争路线内时，开局由 Warden 锁定最快方案；若地图
        # 证明 S02 是高代价支线，则直接交给融合层走更快走廊。
        if self.warden.s02_opening_active(state):
            return self.warden.decide(state)
        optional_bypass = self._optional_s02_bypass_actions(state)
        if optional_bypass:
            return optional_bypass

        if self.mode == self.MODE_PRIMARY:
            # 公开图/必经主墙仍由 Warden 决策，但它也会在途中发出普通点
            # 免费截击卡。复卡状态机必须包住这条入口，不能只在旁路图的
            # MOBILE 分支生效。
            hold = self._mobile_reguard_action(state)
            if hold:
                if state.my_open_contests():
                    return self._replace_main_action(
                        self.warden.decide(state), hold)
                return [hold]
            actions = self.warden.decide(state)
            self._remember_mobile_guard_action(state, actions)
            return actions
        if self.mode == self.MODE_GATE:
            return self.warden.decide(state)

        # 隐藏旁路图也必须完整继承 3.96.34 的 S02 博弈，不能只复用出牌
        # 却让 Planner 决定 PROCESS/WAIT/小分队。换乘完成的当帧立即交回
        # 融合层，避免 Warden 的固定 S10 目标污染隐藏图后续路线。
        s02_handled, s02_actions = self._s02_legacy_actions(state)
        if s02_handled:
            return s02_actions

        self._resolve_s02_outcome(state)
        if self._s02_farm_only:
            self.warden._deny_only_mode = False
            self.warden._score_farm_mode = True
            return self.warden.decide(state)
        if self._s02_deny_only:
            if not self._opponent_can_still_finish(state):
                self._s02_deny_only = False
                self._s02_farm_only = True
                self.warden._deny_only_mode = False
                self.warden._score_farm_mode = True
                return self.warden.decide(state)
            return self._denial_only_actions(state)

        hold = self._mobile_reguard_action(state)
        if hold:
            if state.my_open_contests():
                return self._replace_main_action(
                    self._score_actions(state), hold)
            return [hold]

        # 只要实时 ETA 已经证明我们保有最终墙先手，就启动逐帧领先合同。
        # 不再要求双方恰好同停 S02：明显领先离站、走可选 S02 快线或中途
        # 靠任务/天气建立的先手，都必须服从同一个“保先手内最大化得分”。
        if self._should_preserve_gate_lead(state):
            self._gate_pace_active = True

        actions = self._score_actions(state)
        actions = self._avoid_processed_s02_reentry(state, actions)
        # 固定处理是协议硬前置，不能再被动态截击、移动控路、S14
        # 领先账本或影子追赶替换。03 变种的 S05 死循环就发生在这里。
        main = self._main_action(actions)
        if main and main.get("action") == "PROCESS":
            return actions
        score_plan = self.planner.last_plan
        plan = self._mobile_control_plan(state)
        force_guard = bool(
            plan and plan["denial"]
            and plan["target"] == state.me.get("currentNodeId"))
        intercept = self._score_mobile_intercept(
            state, allow_reserve=force_guard)
        if intercept:
            return self._replace_main_action(actions, intercept)
        if plan:
            mobile_actions = self._mobile_control_actions(state, actions, plan)
            if self._gate_pace_active and not plan.get("denial"):
                main = self._main_action(mobile_actions)
                typ = main.get("action") if main else None
                # 动态卡点本身就是更早的领先合同；若动作已被它的 ETA
                # 证明安全，不再叠加 S14 合同重复收费。只有动态合同没
                # 覆盖住该动作时，才回落最终墙账本兜底。
                mobile_safe = self._action_fits_mobile_lead(
                    state, main, plan)
                if typ != "SET_GUARD" and not mobile_safe:
                    if typ == "MOVE":
                        pace_cost = plan.get("detour", 999)
                    elif typ == "WAIT":
                        pace_cost = 1
                    else:
                        pace_cost = None
                    mobile_actions = self._gate_pace_actions(
                        state, mobile_actions, score_plan,
                        opportunity_cost=pace_cost)
            return mobile_actions

        # 没有更早的 2621 式动态墙时，按 S14 领先预算决定这帧还能不能
        # 得分。威胁进入接管圈或我方已到宫门，才进入不可逆 Warden。
        if self._gate_pace_active:
            if self._gate_pace_expired(state):
                self._gate_pace_active = False
            elif state.me.get("currentNodeId") == state.gate_node \
                    and not state.me.get("routeEdgeId"):
                paced = self._gate_pace_actions(
                    state, actions, score_plan)
                if state.phase != P.PHASE_RUSH \
                        and not self.warden._opp_inbound(
                            state, state.gate_node) \
                        and self._gate_pace_keeps_score_plan(
                            actions, paced, score_plan):
                    return paced
                self._activate_gate_control(state)
                return self.warden.decide(state)
            elif self._should_commit_gate(state):
                # 可选 S02 图从最快走廊建立了逐帧领先账本。即使对手已进
                # 威胁圈，只要当前脚下收益完整塞得进先手，先兑现这一项；
                # 一旦 Planner 无收益或动作被账本替换，立即粘性接管。
                paced = self._gate_pace_actions(
                    state, actions, score_plan)
                if self._gate_pace_keeps_score_plan(
                        actions, paced, score_plan):
                    return paced
                self._activate_gate_control(state)
                return self.warden.decide(state)
            else:
                actions = self._gate_pace_actions(
                    state, actions, score_plan)

        # 固定宫门接管是移动控路找不到更早可靠截击点后的收官方案。
        if self._should_commit_gate(state):
            self._activate_gate_control(state)
            return self.warden.decide(state)
        shadow = self._gate_shadow_race_action(state)
        if shadow:
            return self._replace_main_action(actions, shadow)
        return actions

    def _gate_pace_keeps_score_plan(self, actions, paced, plan):
        """完整得分往返未被账本改写，说明仍保有宫门设卡先手。"""
        if not plan or plan.kind not in ("task", "resource", "bounty"):
            return False
        original = self._main_action(actions)
        kept = self._main_action(paced)
        return bool(original and kept == original
                    and original.get("action") in (
                        "MOVE", "CLAIM_TASK", "CLAIM_RESOURCE",
                        "USE_RESOURCE", "BREAK_GUARD"))

    def _process_required_recovery(self, state):
        """上一帧若暴露处理前置遗漏，本帧无条件恢复到 PROCESS。"""
        me = state.me
        if not me or me.get("routeEdgeId") \
                or me.get("state") in P.BUSY_STATES:
            return None
        if not any(code == P.E_PROCESS_REQUIRED
                   for _, code in state.my_rejections()):
            return None
        actions = []
        contests = state.my_open_contests()
        if contests:
            contest = contests[0]
            actions.append(P.a_window_card(
                contest["contestId"],
                self.warden._defense_card(state, contest)))
        actions.append(P.a_process())
        if self.log:
            self.log.warning(
                "hybrid: recover PROCESS_REQUIRED @%s",
                me.get("currentNodeId"))
        return actions

    def _optional_s02_bypass_actions(self, state):
        """S02 已判定不值得时，禁止 Planner 又被站内资源拉回去。"""
        me = state.me
        if self.warden._s02_opening is not False or not me \
                or me.get("routeEdgeId") \
                or me.get("currentNodeId") != state.start_node:
            return None
        actions = self.warden.decide(state) \
            if self.mode == self.MODE_PRIMARY else self.planner.decide(state)
        main = self._main_action(actions)
        if not main or main.get("action") != "MOVE" \
                or main.get("targetNodeId") != "S02":
            return actions

        boost_type, boost_rem, _ = self.warden._active_speed_buff(state, me)
        _, path = self.warden._travel_dynamic(
            state, state.start_node, state.gate_node,
            boost_type, boost_rem, include_intermediate_process=True,
            conservative_weather=True,
            node_entry_penalty=self.warden._route_entry_penalty(state))
        if len(path) < 2 or path[1] == "S02":
            return actions
        advance = self.warden._advance(state, state.start_node, path[1])
        # 跳过 S02 不是放弃控门，而是从更快走廊开始建立 S14 领先账本：
        # 对手快就同步提速；对手停下来时才把真实余量兑换成任务/资源。
        if self._gate_has_reaction_window(state):
            self._gate_pace_active = True
        if self.log:
            self.log.info(
                "hybrid: optional S02 bypass next=%s planner=%s",
                path[1], main.get("targetNodeId"))
        return self._replace_main_action(actions, advance)

    def _fixed_process_pending(self, state):
        """融合层两套策略任一已确认完成，才允许覆盖固定处理动作。"""
        me = state.me
        if not me or me.get("routeEdgeId"):
            return False
        cur = me.get("currentNodeId")
        node = state.node(cur)
        needs = bool(node.get("processType")
                     and node.get("processType") != "VERIFY"
                     and node.get("processRound", 0) > 0)
        if not needs:
            return False
        planner_done = self.planner._last_stationary_node == cur \
            and self.planner._processed_here
        return not planner_done and not self.warden._node_processed(state, cur)

    def _avoid_processed_s02_reentry(self, state, actions):
        """已完成 S02 后，不为支线目标走回头路并重复换乘。"""
        me = state.me
        main = self._main_action(actions)
        if not me or not main or main.get("action") != "MOVE" \
                or main.get("targetNodeId") != "S02" \
                or me.get("routeEdgeId") or me.get("verified") \
                or me.get("currentNodeId") in (None, "S02") \
                or "S02" not in self.warden._processed_nodes \
                or self._s02_farm_only or self._s02_deny_only:
            return actions
        plan = self.planner.last_plan
        if plan and plan.position == "S02":
            return actions

        cur, gate = me.get("currentNodeId"), state.gate_node
        boost_type, boost_rem, _ = self.warden._active_speed_buff(state, me)
        kwargs = self._gate_travel_kwargs(state)
        direct, direct_path = self.warden._travel_dynamic(
            state, cur, gate, boost_type, boost_rem,
            include_intermediate_process=True,
            conservative_weather=True, **kwargs)
        avoid_kwargs = dict(kwargs)
        blocked = set(avoid_kwargs.get("blocked_nodes") or ())
        blocked.add("S02")
        avoid_kwargs["blocked_nodes"] = blocked
        forward, forward_path = self.warden._travel_dynamic(
            state, cur, gate, boost_type, boost_rem,
            include_intermediate_process=True,
            conservative_weather=True, **avoid_kwargs)
        if not direct_path or len(forward_path) < 2 \
                or forward > direct + self.S02_REENTRY_TOLERANCE:
            return actions

        advance = self.warden._advance(state, cur, forward_path[1])
        if advance.get("action") == "WAIT":
            return actions
        if self.log:
            self.log.info(
                "hybrid: suppress S02 reentry @%s direct=%s forward=%s next=%s",
                cur, direct, forward, forward_path[1])
        return self._replace_main_action(actions, advance)

    def _s02_legacy_actions(self, state):
        me = state.me
        if not me or me.get("routeEdgeId") \
                or me.get("currentNodeId") != "S02" \
                or self.warden._node_processed(state, "S02"):
            return False, []

        # 完成事件必须先吸收再交回融合层。不能先跑完整 Warden 再丢弃
        # 动作：squad_action 会同步扣减内部小分队账本，造成“线上没派、
        # 策略却以为已经派过”的幽灵消耗。
        completed = False
        for event in state.my_events("PROCESS_COMPLETE", "PROCESS_COMPLETED"):
            payload = event.get("payload") or {}
            target = payload.get("targetNodeId") or payload.get("nodeId")
            if target in (None, "S02"):
                completed = True
                break
        if completed:
            self.warden._absorb_feedback(state)
            return False, []

        actions = self.warden.decide(state)
        # 兼容服务端未来新增的完成事件别名；正常完成事件已在上面提前
        # 截获，因此这里不应产生被丢弃的小分队副作用。
        if self.warden._node_processed(state, "S02"):
            return False, []
        return True, actions

    def _resolve_s02_outcome(self, state):
        """S02 处理完成后锁存：正常竞速、只拒止、或双方转农。"""
        if self._s02_farm_only or self._s02_deny_only:
            return
        me = state.me
        if not me or me.get("routeEdgeId") \
                or me.get("currentNodeId") != "S02" \
                or not self.warden._node_processed(state, "S02"):
            return

        remain = state.duration_round - state.round
        my_eta = self._gate_eta(state, me, optimistic=False)
        my_safe = my_eta < 999 \
            and self._my_finish_need(state, my_eta) \
            + self.warden.EXIT_PAD <= remain
        if my_safe:
            self.warden._score_farm_mode = False
            self.warden._deny_only_mode = False
            return

        if self._opponent_can_still_finish(state):
            self._s02_deny_only = True
            self.warden._score_farm_mode = False
            self.warden._deny_only_mode = True
            if self.log:
                self.log.info(
                    "hybrid: S02 outcome=deny-only remain=%d myEta=%s",
                    remain, my_eta)
        else:
            self._s02_farm_only = True
            self.warden._deny_only_mode = False
            self.warden._score_farm_mode = True
            if self.log:
                self.log.info(
                    "hybrid: S02 outcome=farm-only remain=%d myEta=%s",
                    remain, my_eta)

    def _opponent_can_still_finish(self, state):
        """给对手晴天、疾行、零中转处理；仍超时才证明对手也死。"""
        opp = state.opp
        if not opp or opp.get("delivered") or opp.get("retired"):
            return False
        opp_eta = self._gate_eta(state, opp, optimistic=True)
        if opp_eta >= 999:
            return False
        need = self._opponent_finish_lower_bound(state, opp_eta)
        return need <= state.duration_round - state.round

    def _should_commit_denial_gate(self, state):
        """拒止局不要求我方还能交付，只证明能先到且来得及落卡。"""
        me, opp = state.me, state.opp
        if not me or not opp or me.get("verified") \
                or opp.get("verified") or opp.get("delivered") \
                or opp.get("retired"):
            return False
        if not self._gate_has_reaction_window(state):
            return False
        cost = self.warden._guard_base_cost(state, state.gate_node) + 1
        if (me.get("goodFruit", 0) or 0) < cost:
            return False
        my_eta = self._gate_eta(state, me, optimistic=False)
        opp_eta = self._gate_eta(state, opp, optimistic=True)
        if my_eta >= 999:
            return False
        margin = self.warden.MOBILE_GUARD_PAD \
            if self._opponent_gate_committed(state) else self.GATE_LEAD_MARGIN
        return my_eta + margin <= opp_eta

    def _denial_only_actions(self, state):
        """我方交付已死：禁止任务，依次抢动态墙、宫门墙和最近拒止位。"""
        self.warden._deny_only_mode = True
        self.warden._score_farm_mode = False
        hold = self._mobile_reguard_action(state)
        if hold:
            if state.my_open_contests():
                return self._replace_main_action(
                    self.warden.decide(state), hold)
            return [hold]
        actions = self.warden.decide(state)
        main = self._main_action(actions)
        if main and main.get("action") == "PROCESS":
            return actions
        if main and main.get("action") == "SET_GUARD":
            return actions

        intercept = self._score_mobile_intercept(
            state, allow_reserve=True, deny_only=True)
        if intercept:
            return self._replace_main_action(actions, intercept)

        plan = self._mobile_control_plan(state, require_delivery=False)
        if plan:
            return self._mobile_control_actions(state, actions, plan)

        if self._should_commit_denial_gate(state):
            self._activate_gate_control(state)
            self.warden._deny_only_mode = True
            return self.warden.decide(state)

        me = state.me
        if not me or me.get("routeEdgeId") \
                or me.get("state") in P.BUSY_STATES:
            return actions
        cur = me.get("currentNodeId")
        if not cur:
            return actions
        if cur == state.gate_node:
            return self._replace_main_action(actions, P.a_wait())
        advance = self.warden._advance(state, cur, state.gate_node)
        return self._replace_main_action(actions, advance)

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

    @staticmethod
    def _shortest_move_work(state, src, dst):
        """忽略天气/处理的最小移动量；速度时序随后逐帧模拟。"""
        if not src or not dst:
            return math.inf
        if src == dst:
            return 0
        dist = {src: 0}
        queue = [(0, src)]
        while queue:
            moved, cur = heapq.heappop(queue)
            if moved > dist.get(cur, math.inf):
                continue
            if cur == dst:
                return moved
            for nxt, edge in state.graph.neighbors(cur):
                total = moved + state.graph.edge_total_move(edge)
                if total < dist.get(nxt, math.inf):
                    dist[nxt] = total
                    heapq.heappush(queue, (total, nxt))
        return math.inf

    def _opponent_legal_gate_eta(self, state, player):
        """按规则可实现的对手最快宫门 ETA，不把未来疾行提前到当前帧。

        仍给对手最有利条件：晴天、零障碍、零中转处理、资源瞬时启用，
        并允许马的有效帧避开疾行窗口（比真实执行更快，保证下界安全）。
        """
        moving = bool(player.get("routeEdgeId") and player.get("nextNodeId"))
        anchor = player.get("nextNodeId") if moving \
            else player.get("currentNodeId")
        if not anchor:
            return 999

        work = self._shortest_move_work(state, anchor, state.gate_node)
        if work == math.inf:
            return 999
        if moving:
            edge = state.graph.edges.get(player.get("routeEdgeId"))
            total = player.get("edgeTotalMs")
            if not total and edge:
                total = state.graph.edge_total_move(edge)
            work += max(0, (total or 0)
                        - (player.get("edgeProgressMs") or 0))

        resources = player.get("resources") or {}

        def count(resource_type):
            try:
                return max(0, int(resources.get(resource_type, 0) or 0))
            except (TypeError, ValueError):
                return 0

        fast_frames = count(P.FAST_HORSE) * self.warden._opening_horse_duration(
            P.FAST_HORSE)
        short_frames = count(P.SHORT_HORSE) * self.warden._opening_horse_duration(
            P.SHORT_HORSE)
        rush_frames = 0
        active_rush = False
        defaults = {
            P.FAST_HORSE: self.warden._opening_horse_duration(P.FAST_HORSE),
            P.SHORT_HORSE: self.warden._opening_horse_duration(P.SHORT_HORSE),
            P.RUSH_SPEED: RUSH_SPEED_FRAMES,
        }
        for buff in player.get("buffs") or []:
            typ = buff.get("type") or buff.get("buffType")
            if typ not in defaults:
                continue
            remaining = self.warden._buff_remaining(buff, defaults[typ])
            if typ == P.FAST_HORSE:
                fast_frames += remaining
            elif typ == P.SHORT_HORSE:
                short_frames += remaining
            else:
                rush_frames += remaining
                active_rush = active_rush or remaining > 0

        used = player.get("rushTacticUsedCount")
        future_rush = not active_rush and (used is None or used == 0)
        frames = self._visible_process_remain(player)
        while work > 0 and frames < 2000:
            absolute_round = state.round + frames
            if future_rush and absolute_round >= RUSH_EARLIEST:
                rush_frames += RUSH_SPEED_FRAMES
                future_rush = False
            if rush_frames > 0:
                speed = P.SPEED_RUSH
                rush_frames -= 1
            elif fast_frames > 0:
                speed = P.SPEED_FAST_HORSE
                fast_frames -= 1
            elif short_frames > 0:
                speed = P.SPEED_SHORT_HORSE
                short_frames -= 1
            else:
                speed = P.BASE_SPEED
            work -= speed
            frames += 1
        return frames if work <= 0 else 999

    def _gate_eta(self, state, player, optimistic=False):
        if not player or player.get("delivered") or player.get("retired"):
            return 999
        if optimistic:
            return self._opponent_legal_gate_eta(state, player)
        moving = bool(player.get("routeEdgeId") and player.get("nextNodeId"))
        anchor = player.get("nextNodeId") if moving \
            else player.get("currentNodeId")
        if not anchor:
            return 999

        boost_type, boost_rem, _ = self.warden._active_speed_buff(
            state, player)
        if moving:
            if state.enemy_guard(anchor):
                return 999
            edge_frames, boost_type, boost_rem = \
                self._conservative_edge_remaining(
                    state, player, boost_type, boost_rem)
            if edge_frames >= 999:
                return 999
        else:
            edge_frames = 0

        include_current = False
        if moving:
            include_current = anchor != state.gate_node
        elif player.get("playerId") == state.player_id:
            node = state.node(anchor)
            planner_done = self.planner._last_stationary_node == anchor \
                and self.planner._processed_here
            warden_done = self.warden._node_processed(state, anchor)
            include_current = bool(
                node.get("processType")
                and node.get("processType") != "VERIFY"
                and not planner_done and not warden_done)

        travel_kwargs = self._gate_travel_kwargs(state)
        eta, path = self.warden._travel_dynamic(
            state, anchor, state.gate_node, boost_type, boost_rem,
            start_elapsed=edge_frames,
            include_current_process=include_current,
            include_intermediate_process=True,
            conservative_weather=True, **travel_kwargs)
        if player.get("playerId") == state.player_id \
                and not self._gate_route_executable(
                    state, anchor, path, moving=moving):
            return 999
        return eta if path else 999

    def _conservative_edge_remaining(self, state, player,
                                     boost_type, boost_rem):
        """我方已经在边上时，按有限增益和最坏公开天气逐帧走完余量。"""
        edge = state.graph.edges.get(player.get("routeEdgeId"))
        if not edge:
            return 999, None, 0
        need = max(0, (player.get("edgeTotalMs") or 0)
                   - (player.get("edgeProgressMs") or 0))
        moved = frames = 0
        while moved < need and frames < 1000:
            speed = self.warden._boost_speed(boost_type) \
                if boost_rem > 0 else P.BASE_SPEED
            tax = self.warden._weather_tax_at(
                state, edge.get("routeType"), state.round + frames,
                conservative=True, worst_unknown_weather=True)
            moved += max(1, int(speed * 1000 / tax))
            frames += 1
            if boost_rem > 0:
                boost_rem -= 1
                if boost_rem <= 0:
                    boost_type = None
        return frames, boost_type, boost_rem

    def _gate_guard_invest(self, state, node_id, good, bad):
        """保住宫门卡底仓后，是否能用一拍确定拆掉公开敌卡。"""
        guard = state.enemy_guard(node_id)
        if not guard:
            return 0, 0
        defense = guard.get("defense", 0) or 0
        max_good = min(2, max(0, good - self.GATE_GOOD_FRUIT_FLOOR))
        best = None
        for bad_used in range(min(2, bad) + 1):
            for good_used in range(max_good + 1):
                if good_used + bad_used <= 0:
                    continue
                if good_used * 2 + bad_used * 3 < defense:
                    continue
                rank = (good_used, bad_used)
                if best is None or rank < best:
                    best = rank
        return best

    def _gate_travel_kwargs(self, state):
        """保证型 ETA：障碍计有界通行税，仅无确定拆法的敌卡才断路。"""
        good = state.me.get("goodFruit", 0) or 0
        bad = state.me.get("badFruit", 0) or 0
        blocked = set()
        entry_penalty = {}
        for node_id in state.static_nodes:
            obstacle = state.has_obstacle(node_id)
            guard = state.enemy_guard(node_id)
            penalty = 8 if obstacle else 0
            if guard:
                invest = self._gate_guard_invest(
                    state, node_id, good, bad)
                if invest is None:
                    blocked.add(node_id)
                    continue
                penalty += 1             # BREAK_GUARD 本身占一帧主动作
            if penalty:
                entry_penalty[node_id] = penalty
        return {
            "worst_unknown_weather": True,
            "blocked_nodes": blocked,
            "node_entry_penalty": lambda node_id: entry_penalty.get(node_id, 0),
        }

    def _gate_route_executable(self, state, anchor, path, moving=False):
        """校验所选保证路线具备真实动作/资源链，不只在图上有数值。"""
        if not path:
            return False
        good = state.me.get("goodFruit", 0) or 0
        bad = state.me.get("badFruit", 0) or 0
        last_forced = self.planner._last_forced_node \
            or self.warden._last_forced_node
        forced_here = bool(not moving and anchor == last_forced)
        for node_id in path[1:]:
            guard = state.enemy_guard(node_id)
            if guard:
                invest = self._gate_guard_invest(
                    state, node_id, good, bad)
                if invest is None:
                    return False
                good_used, bad_used = invest
                good -= good_used
                bad -= bad_used
            if state.has_obstacle(node_id):
                if forced_here:
                    # 连续强通被 6.3.2 禁止，只能花 1 好果清障后普通移动。
                    if good - 1 < self.GATE_GOOD_FRUIT_FLOOR:
                        return False
                    good -= 1
                    forced_here = False
                else:
                    forced_here = True
            else:
                forced_here = False
        return True

    def _my_finish_need(self, state, gate_eta):
        gate_term, path = self.warden._travel_dynamic(
            state, state.gate_node, state.terminal_node,
            conservative_weather=True, **self._gate_travel_kwargs(state))
        if not path or not self._gate_route_executable(
                state, state.gate_node, path):
            return 999
        rush_wait = 0
        if state.phase != P.PHASE_RUSH:
            rush_wait = max(0, RUSH_EARLIEST - (state.round + gate_eta))
        return gate_eta + rush_wait + self.warden._gate_verify_frames(state) \
            + gate_term + DELIVER_FRAMES

    def _opponent_finish_lower_bound(self, state, gate_eta):
        """对手从现在到交付的规则下界，用于证明一张墙能否锁死。"""
        gate_term, path = self.warden._travel_dynamic(
            state, state.gate_node, state.terminal_node,
            P.RUSH_SPEED, RUSH_SPEED_FRAMES,
            include_intermediate_process=False,
            conservative_weather=False)
        if not path:
            return 999
        rush_wait = 0
        if state.phase != P.PHASE_RUSH:
            rush_wait = max(0, RUSH_EARLIEST - (state.round + gate_eta))
        return gate_eta + rush_wait + self.warden._gate_verify_frames(state) \
            + gate_term + DELIVER_FRAMES

    def _score_mobile_intercept(self, state, allow_reserve=False,
                                deny_only=False):
        """已经占住对手下一站时，立即兑现 2621 式反应卡。"""
        me = state.me
        if not me or me.get("verified") or me.get("delivered") \
                or me.get("retired"):
            return None
        if self._fixed_process_pending(state):
            return None
        my_eta = self._gate_eta(state, me, optimistic=False)
        finish_need = self._my_finish_need(state, my_eta)
        slack = 999 if deny_only else state.duration_round - state.round \
            - finish_need - self.warden.EXIT_PAD
        action = self.warden.mobile_intercept_action(
            state, slack, allow_reserve=allow_reserve)
        if action:
            self._remember_mobile_guard_action(state, [action])
        return action

    def _remember_mobile_guard_action(self, state, actions):
        """锁存任意策略入口刚提交的普通点免费截击卡。"""
        main = self._main_action(actions)
        if not main or main.get("action") != "SET_GUARD":
            return
        me = state.me
        node_id = main.get("targetNodeId")
        extra = main.get("extraGoodFruit", 0) or 0
        # 只把对手已经踏边后提交的普通节点免费卡升级为有界驻守。
        # 关键关、宫门和提前埋伏仍由原 Warden 生命周期管理。
        if not me or me.get("routeEdgeId") \
                or node_id != me.get("currentNodeId") \
                or self.warden._guard_base_cost(state, node_id) + extra != 0 \
                or not self.warden._opp_inbound(state, node_id):
            return
        self._mobile_hold_node = node_id

    def _mobile_reguard_safe(self, state, node_id):
        """免费移动卡可续守一帧，且仍保住交付与最终墙先手。"""
        if not node_id or not self.warden._opp_inbound(state, node_id):
            return False
        extra = self.warden._mobile_guard_extra(state, node_id)
        if self.warden._guard_base_cost(state, node_id) + extra != 0:
            return False
        edge_remain = self.warden._opp_edge_remaining(state)
        if edge_remain < self.warden.MOBILE_GUARD_PAD:
            return False
        delay = self.warden._mobile_guard_delay(
            state, node_id, extra, edge_remain)
        if delay < self.warden.MOBILE_GUARD_MIN_DELAY:
            return False

        if self._s02_deny_only or self.warden._deny_only_mode:
            return True
        if not self._gate_has_reaction_window(state):
            return False
        my_eta = self._gate_eta(state, state.me, optimistic=False)
        if my_eta >= 999:
            return False
        finish_need = self._my_finish_need(state, my_eta)
        remain = state.duration_round - state.round
        # WAIT 本身也占一帧，不能把 EXIT_PAD 的最后一帧拿去陪卡。
        if finish_need + self.warden.EXIT_PAD + 1 > remain:
            return False
        return self._gate_lead_budget(state) >= 1

    def _mobile_hold_score_action(self, state, node_id):
        """有效移动墙已成立时，把确定安全的等待帧兑换成脚下收益。"""
        action = self.warden._farm_here_safe(state, node_id)
        if not action:
            return None
        typ = action.get("action")
        if typ == "CLAIM_TASK":
            task = next((t for t in state.claimable_tasks()
                         if t.get("taskId") == action.get("taskId")), None)
            cost = (task.get("processRound", 4) if task else 4) + 1
        elif typ == "CLAIM_RESOURCE":
            cost = 2
        else:
            return None
        # 即使卡在读条第一帧被远程拆掉，任务结束后仍至少留出完整的
        # T->T+5 复卡窗；否则继续 WAIT，绝不拿任务赌掉墙权。
        if self.warden._opp_edge_remaining(state) \
                < cost + self.warden.MOBILE_GUARD_PAD:
            return None
        return action

    def _mobile_reguard_action(self, state):
        """对手仍在入边时守住免费卡；拆掉后仍在边上则立即复卡。"""
        if self._fixed_process_pending(state):
            return None
        node_id = self._mobile_hold_node
        if not node_id:
            # 不把复卡链只绑在上一帧的 Python 内存标记上。首次卡可能由
            # 其它合法入口提交，或完成事件与动作状态跨帧交错；现场已有
            # 我方免费卡且对手仍在入边，就是更可靠的恢复证据。
            me = state.me
            cur = me.get("currentNodeId") if me else None
            guard = state.node(cur).get("guard") if cur else None
            initial_defense = (guard or {}).get(
                "initialDefense", (guard or {}).get("defense", 0)) or 0
            if not me or me.get("routeEdgeId") or not cur \
                    or me.get("state") in P.BUSY_STATES \
                    or initial_defense != 2 \
                    or not self.warden._my_active_guard(state, cur) \
                    or not self._mobile_reguard_safe(state, cur):
                return None
            self._mobile_hold_node = node_id = cur
            if self.log:
                self.log.info("hybrid: recovered mobile guard hold @%s", cur)
        me = state.me
        if not me or me.get("routeEdgeId") \
                or me.get("currentNodeId") != node_id \
                or me.get("verified") or me.get("delivered") \
                or me.get("retired"):
            self._mobile_hold_node = None
            return None
        if me.get("state") in P.BUSY_STATES:
            return None
        if not self._mobile_reguard_safe(state, node_id):
            self._mobile_hold_node = None
            return None

        guard = state.node(node_id).get("guard")
        active = bool(guard and guard.get("ownerTeamId") == state.my_team
                      and guard.get("active", guard.get("defense", 0) > 0))
        if active:
            score = self._mobile_hold_score_action(state, node_id)
            return score or P.a_wait()

        action = self._score_mobile_intercept(
            state,
            allow_reserve=self._s02_deny_only or self.warden._deny_only_mode,
            deny_only=self._s02_deny_only or self.warden._deny_only_mode)
        if action:
            return action
        self._mobile_hold_node = None
        return None

    @staticmethod
    def _replace_main_action(actions, replacement):
        auxiliary = [a for a in actions
                     if a.get("action") not in P.MAIN_ACTION_TYPES]
        return [replacement] + auxiliary

    def _mobile_control_plan(self, state, require_delivery=True):
        """选择我方能先到、对手绕开也会付税的最近汇合点。"""
        me, opp = state.me, state.opp
        if not me or not opp or state.my_open_contests():
            return None
        if me.get("state") in P.BUSY_STATES or me.get("routeEdgeId") \
                or me.get("verified") or me.get("delivered") \
                or me.get("retired"):
            return None
        if opp.get("delivered") or opp.get("retired"):
            return None

        cur = me.get("currentNodeId")
        origin = opp.get("currentNodeId")
        anchor = opp.get("nextNodeId")
        gate = state.gate_node
        if not cur or not origin or not gate \
                or cur in (state.start_node, "S02", gate,
                           state.terminal_node):
            return None
        if not opp.get("routeEdgeId") or not opp.get("nextNodeId"):
            return self._held_mobile_plan(
                state, require_delivery=require_delivery)

        edge_remain = self._edge_remaining_frames(opp, optimistic=True)
        opp_gate, opp_path = self.warden._shortest(
            state, anchor, gate, P.SPEED_RUSH)
        my_boost, my_boost_rem, _ = self.warden._active_speed_buff(
            state, me)
        travel_kwargs = self._gate_travel_kwargs(state)
        my_gate, my_path = self.warden._travel_dynamic(
            state, cur, gate, my_boost, my_boost_rem,
            include_intermediate_process=True,
            conservative_weather=True, **travel_kwargs)
        if not opp_path or not my_path:
            return None

        remain = state.duration_round - state.round
        opp_gate_eta = edge_remain + opp_gate
        opp_finish = self._opponent_finish_lower_bound(
            state, opp_gate_eta)
        candidates = []
        for node_id in opp_path[:-1]:
            node_type = state.node(node_id).get("nodeType")
            if node_id in (state.start_node, "S02", gate,
                           state.terminal_node) \
                    or node_type in ("GATE", "TERMINAL", "START"):
                continue
            guard = state.node(node_id).get("guard")
            if guard and guard.get("active", guard.get("defense", 0) > 0):
                continue

            my_eta, my_route, after_type, after_rem = \
                self.warden._travel_dynamic(
                    state, cur, node_id, my_boost, my_boost_rem,
                    include_intermediate_process=True,
                    conservative_weather=True, return_boost=True,
                    **travel_kwargs)
            opp_leg, opp_route = self.warden._shortest(
                state, anchor, node_id, P.SPEED_RUSH)
            if not my_route or not opp_route:
                continue
            opp_eta = edge_remain + opp_leg
            if my_eta + self.warden.MOBILE_GUARD_PAD > opp_eta:
                continue

            extra = self.warden._mobile_guard_extra(state, node_id)
            cost = self.warden._guard_base_cost(state, node_id) + extra

            if node_id == anchor:
                alt, alt_path = self.warden._shortest_avoiding(
                    state, origin, gate, node_id, P.SPEED_RUSH)
                direct = edge_remain + opp_gate
            else:
                alt, alt_path = self.warden._shortest_avoiding(
                    state, anchor, gate, node_id, P.SPEED_RUSH)
                direct = opp_gate
            reroute_delay = max(0, alt - direct) if alt_path else 999
            # 卡只在对手踏上最后一条入边时落下；风化只扣这条入边，而不是
            # 从现在到汇合点的全部路程。
            if node_id == anchor:
                guard_exposure = edge_remain
            else:
                prev = opp_route[-2]
                inbound = state.graph.edge_between(prev, node_id)
                guard_exposure = state.graph.edge_frames(
                    inbound, P.SPEED_RUSH) if inbound else opp_eta - my_eta
            stay_delay = self.warden._mobile_stay_delay(
                state, node_id, extra, guard_exposure)
            delay = min(stay_delay, reroute_delay)
            if delay < self.warden.MOBILE_GUARD_MIN_DELAY:
                continue

            target_needs_process = bool(
                state.node(node_id).get("processType")) \
                and (node_id != cur or not self.planner._processed_here)
            via_gate, after_path = self.warden._travel_dynamic(
                state, node_id, gate, after_type, after_rem,
                start_elapsed=my_eta,
                include_current_process=target_needs_process,
                include_intermediate_process=True,
                conservative_weather=True, **travel_kwargs)
            if not after_path:
                continue
            detour = max(0, via_gate - my_gate)
            finish_need = self._my_finish_need(state, via_gate)
            if require_delivery \
                    and finish_need + self.warden.EXIT_PAD > remain:
                continue

            delayed_finish = self._opponent_finish_lower_bound(
                state, opp_gate_eta + delay)
            denial = opp_finish <= remain and delayed_finish > remain
            # 确定能把对手锁过死线时，允许动用底仓并接受更长绕路；否则
            # 仍执行资源与绕路纪律，避免把“尽力堵”退化为无效自残。
            good = me.get("goodFruit", 0) or 0
            reserve = 0 if denial else self.warden.FRUIT_RESERVE
            if good - cost < reserve:
                continue
            if not denial and detour > self.MOBILE_APPROACH_MAX_DETOUR:
                continue

            finish_tax = max(0, delayed_finish - opp_finish)
            globally_mandatory = not self._reachable_without(
                state, state.start_node, gate, node_id)
            rank = (0 if denial else 1, opp_eta, detour,
                    -finish_tax, -delay, node_id)
            candidates.append((rank, {
                "target": node_id, "myEta": my_eta, "oppEta": opp_eta,
                "delay": delay, "finishTax": finish_tax,
                "stayDelay": stay_delay, "rerouteDelay": reroute_delay,
                "globallyMandatory": globally_mandatory,
                "detour": detour, "denial": denial,
                "myFinish": finish_need, "oppBaseFinish": opp_finish,
                "oppFinish": delayed_finish, "guardCost": cost,
            }))

        if not candidates:
            return self._held_mobile_plan(
                state, require_delivery=require_delivery)
        return min(candidates, key=lambda item: item[0])[1]

    def _held_mobile_plan(self, state, require_delivery=True):
        """对手短暂停站时守住已抢墙位；确认改线或触碰死线才解除。"""
        target = self.mobile_target
        previous = self.mobile_plan
        me, opp = state.me, state.opp
        if not target or not previous or not me or not opp:
            return None
        cur = me.get("currentNodeId")
        opp_pos = opp.get("nextNodeId") or opp.get("currentNodeId")
        gate = state.gate_node
        if not cur or not opp_pos or me.get("routeEdgeId"):
            return None

        my_boost, my_boost_rem, _ = self.warden._active_speed_buff(
            state, me)
        travel_kwargs = self._gate_travel_kwargs(state)
        my_eta, my_path, after_type, after_rem = \
            self.warden._travel_dynamic(
                state, cur, target, my_boost, my_boost_rem,
                include_intermediate_process=True,
                conservative_weather=True, return_boost=True,
                **travel_kwargs)
        opp_eta, opp_path = self.warden._shortest(
            state, opp_pos, target, P.SPEED_RUSH)
        opp_gate, gate_path = self.warden._shortest(
            state, opp_pos, gate, P.SPEED_RUSH)
        target_gate, target_path = self.warden._shortest(
            state, target, gate, P.SPEED_RUSH)
        if not my_path or not opp_path or not gate_path or not target_path:
            self.mobile_target = self.mobile_plan = None
            return None
        if opp_eta + target_gate > opp_gate + self.MOBILE_ROUTE_TOLERANCE:
            self.mobile_target = self.mobile_plan = None
            return None
        if my_eta + self.warden.MOBILE_GUARD_PAD > opp_eta:
            self.mobile_target = self.mobile_plan = None
            return None

        target_needs_process = bool(
            state.node(target).get("processType")) \
            and (target != cur or not self.planner._processed_here)
        my_gate_eta, my_gate_path = self.warden._travel_dynamic(
            state, target, gate, after_type, after_rem,
            start_elapsed=my_eta,
            include_current_process=target_needs_process,
            include_intermediate_process=True,
            conservative_weather=True, **travel_kwargs)
        remain = state.duration_round - state.round
        finish_need = self._my_finish_need(state, my_gate_eta)
        if not my_gate_path or (require_delivery
                                and finish_need + self.warden.EXIT_PAD > remain):
            self.mobile_target = self.mobile_plan = None
            return None

        held = dict(previous)
        held.update(myEta=my_eta, oppEta=opp_eta,
                    myFinish=finish_need, held=True)
        return held

    @staticmethod
    def _main_action(actions):
        return next((a for a in actions
                     if a.get("action") in P.MAIN_ACTION_TYPES), None)

    def _action_fits_mobile_lead(self, state, action, plan):
        if not action:
            return False
        typ = action.get("action")
        if typ == "PROCESS":
            return True                    # 固定处理不完成，本来也无法离站
        if typ == "MOVE":
            nxt = self.warden._next_hop(
                state, state.me.get("currentNodeId"), plan["target"],
                state.my_speed())
            return bool(nxt and action.get("targetNodeId") == nxt)
        cost = None
        if typ == "CLAIM_TASK":
            task = next((t for t in state.claimable_tasks()
                         if t.get("taskId") == action.get("taskId")), None)
            if task and task.get("nodeId") == state.me.get("currentNodeId"):
                cost = (task.get("processRound", 4) or 4) + 1
        elif typ == "CLAIM_RESOURCE" \
                and action.get("targetNodeId") == state.me.get("currentNodeId"):
            cost = 2
        elif typ == "USE_RESOURCE":
            cost = 1
        elif typ == "WAIT":
            cost = 1
        return cost is not None and plan["myEta"] + cost \
            + self.warden.MOBILE_GUARD_PAD <= plan["oppEta"]

    def _mobile_control_actions(self, state, actions, plan=None):
        """主动奔赴截击点；脚下收益能塞进先手窗口时继续交给 Planner。"""
        plan = plan or self._mobile_control_plan(state)
        if not plan:
            self.mobile_target = self.mobile_plan = None
            return actions
        self.mobile_target = plan["target"]
        self.mobile_plan = dict(plan)
        main = self._main_action(actions)
        if self._action_fits_mobile_lead(state, main, plan):
            return actions

        cur = state.me.get("currentNodeId")
        if cur == plan["target"]:
            return self._replace_main_action(actions, P.a_wait())
        nxt = self.warden._next_hop(
            state, cur, plan["target"], state.my_speed())
        if not nxt or state.has_obstacle(nxt) or state.enemy_guard(nxt):
            return actions
        if self.log:
            self.log.info(
                "hybrid: mobile target=%s eta=%s/%s tax=%s "
                "(stay=%s reroute=%s mandatory=%s) denial=%s",
                plan["target"], plan["myEta"], plan["oppEta"],
                plan["delay"], plan.get("stayDelay"),
                plan.get("rerouteDelay"), plan.get("globallyMandatory"),
                plan["denial"])
        return self._replace_main_action(actions, P.a_move(nxt))

    def _score_actions(self, state):
        """Planner 管得分，但 S02 继续执行 3.96.34 的不认输出牌。"""
        actions = self.planner.decide(state)
        contests = state.my_open_contests()
        s02 = next((c for c in contests
                    if c.get("targetNodeId") == "S02"
                    and c.get("contestType")
                    in (P.CONTEST_DOCK, P.CONTEST_TASK)), None)
        if not s02:
            return actions

        card_action = P.a_window_card(
            s02["contestId"], self.warden._defense_card(state, s02))
        # 每帧只发一张窗口牌；S02 持续争夺时由这条策略独占窗口动作。
        actions = [a for a in actions if a.get("action") != "WINDOW_CARD"]
        return [card_action] + actions

    def _opponent_gate_committed(self, state):
        opp = state.opp
        if not opp or not opp.get("routeEdgeId") or not opp.get("nextNodeId"):
            return False
        cur, nxt = opp.get("currentNodeId"), opp.get("nextNodeId")
        edge = state.graph.edges.get(opp.get("routeEdgeId"))
        if not cur or not edge:
            return False
        cur_eta, cur_path = self.warden._travel_dynamic(
            state, cur, state.gate_node,
            P.RUSH_SPEED, RUSH_SPEED_FRAMES,
            include_intermediate_process=False,
            conservative_weather=False)
        next_eta, next_path = self.warden._travel_dynamic(
            state, nxt, state.gate_node,
            P.RUSH_SPEED, RUSH_SPEED_FRAMES,
            include_intermediate_process=False,
            conservative_weather=False)
        edge_eta = state.graph.edge_frames(edge, P.SPEED_RUSH)
        return bool(cur_path and next_path) \
            and edge_eta + next_eta <= cur_eta + self.MOBILE_ROUTE_TOLERANCE

    def _should_preserve_gate_lead(self, state):
        """实时证明存在宫门先手后启动节奏账本，不依赖特定站位。"""
        me, opp = state.me, state.opp
        if not me or not opp or self.mode != self.MODE_MOBILE \
                or self._s02_farm_only or self._s02_deny_only \
                or self.warden._score_farm_mode \
                or self.warden._deny_only_mode:
            return False
        if me.get("verified") or me.get("delivered") or me.get("retired"):
            return False
        if opp.get("verified") or opp.get("delivered") or opp.get("retired"):
            return False
        if (me.get("goodFruit", 0) or 0) < self.GATE_GOOD_FRUIT_FLOOR:
            return False
        if not self._gate_has_reaction_window(state):
            return False
        my_eta = self._gate_eta(state, me, optimistic=False)
        remain = state.duration_round - state.round
        if my_eta >= 999 or self._my_finish_need(state, my_eta) \
                + self.warden.EXIT_PAD > remain:
            return False
        # 对手按晴天、合法疾行时序、零中转处理下界；我方按天气、有限
        # 增益、障碍/敌卡和处理站上界。非负预算是真墙权；此外我方刚在
        # S02 完成处理、对手仍停在 S02 时启动竞速保险，把不足 5 帧的
        # 小先手扩大成可落卡先手，而不是误称已经拥有墙权。
        if self._gate_lead_budget(state) >= 0:
            return True
        return bool(not me.get("routeEdgeId")
                    and me.get("currentNodeId") == "S02"
                    and self.warden._node_processed(state, "S02")
                    and not opp.get("routeEdgeId")
                    and opp.get("currentNodeId") == "S02")

    def _gate_pace_expired(self, state):
        me, opp = state.me, state.opp
        return (not me or not opp or me.get("verified")
                or me.get("delivered") or me.get("retired")
                or opp.get("verified") or opp.get("delivered")
                or opp.get("retired")
                or not self._gate_has_reaction_window(state))

    @staticmethod
    def _visible_process_remain(player):
        """只计公开且已经开始、无法拿回的处理帧。"""
        if not player or player.get("routeEdgeId"):
            return 0
        process = player.get("currentProcess") or {}
        for key in ("remainRound", "remainingRound", "remain"):
            if process.get(key) is not None:
                try:
                    return max(0, int(process[key]))
                except (TypeError, ValueError):
                    return 0
        return 0

    def _gate_lead_budget(self, state):
        """仍能保证先完成设卡的可消费帧数；负数表示必须立即追门。"""
        my_eta = self._gate_eta(state, state.me, optimistic=False)
        opp_eta = self._gate_eta(state, state.opp, optimistic=True)
        # 对手 ETA 按合法时序的速度下界：当前马可立即用，疾行最早 r390；
        # 晴天、零障碍、零中转处理仍全部让利给对手。
        margin = self.warden.MOBILE_GUARD_PAD \
            if self._opponent_gate_committed(state) else self.GATE_LEAD_MARGIN
        return opp_eta - my_eta - margin

    def _gate_plan_opportunity_cost(self, state, plan):
        """候选相对直接去 S14 多花的完整帧数（路程、读条、天气、马）。"""
        if not plan:
            return 999
        if plan.kind == "deliver":
            return 0
        if plan.kind == "hold" or not plan.position:
            return 999

        me = state.me
        cur, target, gate = (me.get("currentNodeId"), plan.position,
                             state.gate_node)
        if not cur or me.get("routeEdgeId"):
            return 999
        direct = self._gate_eta(state, me, optimistic=False)
        travel_kwargs = self._gate_travel_kwargs(state)
        boost_type, boost_rem, _ = self.warden._active_speed_buff(state, me)
        to_target, to_path, after_type, after_rem = self.warden._travel_dynamic(
            state, cur, target, boost_type, boost_rem,
            include_intermediate_process=True,
            conservative_weather=True, return_boost=True, **travel_kwargs)
        if not to_path:
            return 999

        fixed_process = 0
        if target != cur or not self.planner._processed_here:
            fixed_process = self.warden._node_process_frames_at(
                state, target, state.round + to_target,
                worst_unknown_weather=True)
        if plan.kind == "task" and plan.task:
            process = (plan.task.get("processRound", 4) or 4) + 1
        elif plan.kind == "resource":
            process = 2
        elif plan.kind == "bounty":
            process = 1
        else:
            return 999
        process += fixed_process
        after_type, after_rem = self.warden._consume_boost(
            after_type, after_rem, process)
        via, via_path = self.warden._travel_dynamic(
            state, target, gate, after_type, after_rem,
            start_elapsed=to_target + process,
            include_intermediate_process=True,
            conservative_weather=True, **travel_kwargs)
        if not via_path:
            return 999
        # S02 的固定换乘在每次重新进站时都会再读条。保领先阶段禁止为了
        # 支线收益穿回已经完成的 S02；纯农模式不走这套宫门预算。
        if "S02" in self.warden._processed_nodes:
            outbound_reentry = cur != "S02" and "S02" in to_path[1:]
            return_reentry = target != "S02" and "S02" in via_path[1:]
            if outbound_reentry or return_reentry:
                return 999
        return max(0, via - direct) if direct < 999 else 999

    def _gate_pace_actions(self, state, actions, plan, opportunity_cost=None):
        """任务吃得下领先就做；吃不下时只替换主动作，保留合法辅动作。"""
        me = state.me
        if me.get("routeEdgeId") or me.get("state") in P.BUSY_STATES:
            return actions
        main = self._main_action(actions)
        if main and main.get("action") == "PROCESS":
            return actions                 # 固定处理未完成时本来也无法离站
        if main and main.get("action") in ("RUSH_SPEED",):
            return actions
        if main and main.get("action") == "USE_RESOURCE" \
                and main.get("resourceType") in (
                    P.FAST_HORSE, P.SHORT_HORSE, P.RUSH_SPEED):
            return actions

        budget = self._gate_lead_budget(state)
        cost = self._gate_plan_opportunity_cost(state, plan) \
            if opportunity_cost is None else opportunity_cost
        if main:
            typ = main.get("action")
            if typ == "WAIT":
                cost = max(cost, 1)
            elif typ == "CLAIM_RESOURCE":
                cost = max(cost, 2)
            elif typ == "CLAIM_TASK":
                task = next((t for t in state.claimable_tasks()
                             if t.get("taskId") == main.get("taskId")), None)
                if task:
                    cost = max(cost,
                               (task.get("processRound", 4) or 4) + 1)
            elif typ == "USE_RESOURCE":
                cost = max(cost, 1)
        if cost <= budget:
            return actions

        cur = me.get("currentNodeId")
        if not cur:
            return actions
        if cur == state.gate_node:
            # 已在宫门时无法再“向宫门推进”。原样返回会被上层误判为
            # 得分动作安全，进而离开 S14。改成 WAIT 让调用方识别合同
            # 不成立并当帧粘性切入 Gate Warden。
            return self._replace_main_action(actions, P.a_wait())
        advance = self.warden._advance(state, cur, state.gate_node)
        if advance.get("action") == "WAIT":
            return actions
        if self.log:
            self.log.info(
                "hybrid: preserve gate lead budget=%s task_cost=%s @%s",
                budget, cost, cur)
        return self._replace_main_action(actions, advance)

    def _gate_shadow_race_action(self, state):
        """对手已冲向终局时跟住 S14 竞速，禁止陷阱层在旁路口罚站。"""
        me, opp = state.me, state.opp
        if not me or not opp or me.get("routeEdgeId") \
                or me.get("state") in P.BUSY_STATES:
            return None
        if me.get("verified") or me.get("delivered") or me.get("retired"):
            return None
        if not self._gate_has_reaction_window(state) \
                or not self._opponent_gate_committed(state):
            return None
        my_eta = self._gate_eta(state, me, optimistic=False)
        opp_eta = self._gate_eta(state, opp, optimistic=True)
        if opp_eta > self.GATE_THREAT_ETA \
                or my_eta > opp_eta + self.GATE_SHADOW_TRAIL_MAX:
            return None
        remain = state.duration_round - state.round
        if self._my_finish_need(state, my_eta) + self.warden.EXIT_PAD > remain:
            return None
        cur = me.get("currentNodeId")
        if not cur or cur == state.gate_node:
            return None
        action = self.warden._advance(state, cur, state.gate_node)
        return action if action.get("action") != "WAIT" else None

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
        lead_margin = self.warden.MOBILE_GUARD_PAD \
            if self._opponent_gate_committed(state) else self.GATE_LEAD_MARGIN
        if my_eta + lead_margin > opp_eta:
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

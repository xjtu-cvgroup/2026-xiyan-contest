"""隐藏地图混合策略：固定主墙 + 2621 移动守望者。

战略按地图拓扑切换控制方式：
- 当前地图存在真正必经的 KEY_PASS：完整沿用 Warden；
- KEY_PASS 可绕：移动守望者主动抢汇合点，Planner 负责可塞入余量的任务；
- 移动局后段能严格证明抢到宫门先手：粘性切入 S14 Warden。
"""
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

    def __init__(self, logger=None):
        self.log = logger
        self.planner = PlannerStrategy(logger)
        self.warden = WardenStrategy(logger)
        self.mode = None
        self.primary_choke = None
        self.mobile_target = None
        self.mobile_plan = None
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

        if self.mode in (self.MODE_PRIMARY, self.MODE_GATE):
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

        # S02 已拿到明确处理先手时，开始保护最终墙的领先预算。此处不再
        # 直接粘性切到 S14：只要任务的完整机会成本吃不掉先手，就继续得分。
        if self._should_preserve_s02_gate_lead(state):
            self._gate_pace_active = True

        actions = self._score_actions(state)
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
                # 真落卡会给对手增加延误，不作为纯支出拦截；普通抢位的
                # 绕路和驻守则逐帧从最终墙领先里付款。
                if typ != "SET_GUARD":
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
                self._activate_gate_control(state)
                return self.warden.decide(state)
            elif self._should_commit_gate(state):
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
        actions = self.warden.decide(state)
        main = self._main_action(actions)
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
        if not cur or cur == state.gate_node:
            return actions
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

    def _gate_eta(self, state, player, optimistic=False):
        if not player or player.get("delivered") or player.get("retired"):
            return 999
        moving = bool(player.get("routeEdgeId") and player.get("nextNodeId"))
        anchor = player.get("nextNodeId") if moving \
            else player.get("currentNodeId")
        if not anchor:
            return 999

        if optimistic:
            boost_type, boost_rem = P.RUSH_SPEED, RUSH_SPEED_FRAMES
            edge_frames = self._edge_remaining_frames(player, True) \
                if moving else 0
            boost_type, boost_rem = self.warden._consume_boost(
                boost_type, boost_rem, edge_frames)
        else:
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
            include_current = bool(
                node.get("processType")
                and node.get("processType") != "VERIFY"
                and not self.planner._processed_here)

        travel_kwargs = {} if optimistic else self._gate_travel_kwargs(state)
        eta, path = self.warden._travel_dynamic(
            state, anchor, state.gate_node, boost_type, boost_rem,
            start_elapsed=edge_frames,
            include_current_process=include_current,
            include_intermediate_process=not optimistic,
            conservative_weather=not optimistic, **travel_kwargs)
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

    @staticmethod
    def _gate_travel_kwargs(state):
        """保证型 ETA 不穿越尚未确定能排除的公开阻挡。"""
        blocked = {
            node_id for node_id in state.static_nodes
            if state.has_obstacle(node_id) or state.enemy_guard(node_id)
        }
        return {
            "worst_unknown_weather": True,
            "blocked_nodes": blocked,
        }

    def _my_finish_need(self, state, gate_eta):
        gate_term, path = self.warden._travel_dynamic(
            state, state.gate_node, state.terminal_node,
            conservative_weather=True, **self._gate_travel_kwargs(state))
        if not path:
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
        my_eta = self._gate_eta(state, me, optimistic=False)
        finish_need = self._my_finish_need(state, my_eta)
        slack = 999 if deny_only else state.duration_round - state.round \
            - finish_need - self.warden.EXIT_PAD
        return self.warden.mobile_intercept_action(
            state, slack, allow_reserve=allow_reserve)

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

    def _should_preserve_s02_gate_lead(self, state):
        """换乘完成领先时启动最终墙节奏账本，不在这里直接锁死路线。"""
        me, opp = state.me, state.opp
        if not me or not opp or me.get("routeEdgeId") \
                or me.get("currentNodeId") != "S02":
            return False
        if not self.warden._node_processed(state, "S02"):
            return False
        if opp.get("routeEdgeId") or opp.get("currentNodeId") != "S02":
            return False
        if opp.get("verified") or opp.get("delivered") or opp.get("retired"):
            return False
        if (me.get("goodFruit", 0) or 0) < self.GATE_GOOD_FRUIT_FLOOR:
            return False
        if not self._gate_has_reaction_window(state):
            return False
        my_eta = self._gate_eta(state, me, optimistic=False)
        remain = state.duration_round - state.round
        return my_eta < 999 and self._my_finish_need(state, my_eta) \
            + self.warden.EXIT_PAD <= remain

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
        # 对手 ETA 仍按疾行、晴天、零中转处理的极限下界；唯一加回的是
        # 当前公开读条剩余帧，因为这部分时间已经发生且不能撤销。
        opp_eta += self._visible_process_remain(state.opp)
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
        to_target, path, after_type, after_rem = self.warden._travel_dynamic(
            state, cur, target, boost_type, boost_rem,
            include_intermediate_process=True,
            conservative_weather=True, return_boost=True, **travel_kwargs)
        if not path:
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
        via, path = self.warden._travel_dynamic(
            state, target, gate, after_type, after_rem,
            start_elapsed=to_target + process,
            include_intermediate_process=True,
            conservative_weather=True, **travel_kwargs)
        return max(0, via - direct) if path and direct < 999 else 999

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
        if not cur or cur == state.gate_node:
            return actions
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

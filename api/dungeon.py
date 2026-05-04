"""
活动-地下城-爬楼模块

流程（每层）：
  1. sally/eventsally          — 进入指定楼层（event_layer_id=N）
  2. 循环直到 is_finish=true：
     a. sally/eventforward（direction=0）→ resp 含推荐 formation_id 和 is_finish
     b. battle/battle（formation_id 来自 eventforward resp，游戏自动选择）
  3. sally/resetsallystatus    — 层结算（sword+t，无额外参数；resp 含新 t）
  4. party/getpartyinfo        — 刷新队伍状态选择界面（party_no=3；无 JSON resp）
  重复 1-4，layer_id 自增，直到 DUNGEON_TARGET_LAYER

气力管理：
  每打完 DUNGEON_FLOORS_PER_CYCLE（10）层后归城，在本丸重新排列低气力刀至前列，
  再重新进入继续爬楼。归城 endpoint 待补充。

配置：
  DUNGEON_PARTY_NO           = 3
  DUNGEON_EVENT_ID           = 190
  DUNGEON_EVENT_FIELD_ID     = 1
  DUNGEON_TARGET_LAYER       = 90
  DUNGEON_FATIGUE_THRESHOLD  = 70

约束：
  - formation_id 由 eventforward 响应直接给出（游戏自动推荐），透传给 battle/battle
  - 爬楼结束后调用 run_repair_check 检查队伍3重伤
  - 归城 endpoint 待补充，当前用 get_game_state() 同步状态
"""

import time
from loguru import logger

from .client import ToukenClient
from .battle import api_sleep, battle_sleep, get_party_slots, remove_sword, set_sword, recover_sword_fatigue
from .repair import run_repair_check, run_all_repairs, is_heavily_injured
from .expedition import quick_expedition_check

DUNGEON_PARTY_NO:           int = 3
DUNGEON_EVENT_ID:           int = 190
DUNGEON_EVENT_FIELD_ID:     int = 1
DUNGEON_TARGET_LAYER:       int = 90
DUNGEON_DEFAULT_FORMATION:  int = 1   # eventforward 未给出 formation_id 时的回退值
DUNGEON_FATIGUE_THRESHOLD:  int = 70  # 低于此值的刀优先排到前列 / 触发归城重排
DUNGEON_FATIGUE_CRITICAL:   int = 40  # 低于此值即使只有1把也触发1-1恢复
DUNGEON_FLOORS_PER_CYCLE:   int = 10  # 每打完多少层归城一次调整气力
DUNGEON_REST_SECONDS:       int = 5   # 归城时休息时长


# ── 气力排序 ────────────────────────────────────────────────────

def _sort_fatigued_swords_to_front(client: ToukenClient) -> None:
    """
    检查队伍3，将气力 < DUNGEON_FATIGUE_THRESHOLD 的刀按气力升序排到前列槽位。
    全员正常则跳过 API 操作。
    """
    conquest_data = client.get_conquest_data()
    slots = get_party_slots(conquest_data, DUNGEON_PARTY_NO)
    if not slots:
        return

    # conquest 气力数据可能不准，用 party/getpartyinfo 读取实际气力
    partyinfo_resp = client._post("party/getpartyinfo", extra={"party_no": DUNGEON_PARTY_NO})
    partyinfo_swords = partyinfo_resp.get("sword", {})
    fatigue_map: dict[int, int] = {}
    for sid in slots.values():
        sword_data = partyinfo_swords.get(str(sid), partyinfo_swords.get(sid, {}))
        fatigue_map[sid] = sword_data.get("fatigue", 100)

    ordered_slots = sorted(slots.items())               # [(order, sid), ...]
    current_sids  = [sid for _, sid in ordered_slots]
    orders        = [order for order, _ in ordered_slots]

    low_swords = sorted(
        [(sid, fat) for sid, fat in fatigue_map.items() if fat < DUNGEON_FATIGUE_THRESHOLD],
        key=lambda x: x[1]
    )
    if not low_swords:
        logger.debug("队伍3气力全部正常，无需重排")
        return

    low_sids    = [sid for sid, _ in low_swords]
    normal_sids = [sid for sid in current_sids if sid not in set(low_sids)]
    new_sids    = low_sids + normal_sids

    if new_sids == current_sids:
        logger.debug("重排后顺序不变，跳过 API 操作")
        return

    logger.info(
        f"队伍3有 {len(low_swords)} 把刀气力 <{DUNGEON_FATIGUE_THRESHOLD}，重排至前列..."
    )

    current_slot1_sid = slots.get(1)

    # 优化：只有1把低气力刀且不在槽1 → 直接 swap，1次API代替12次
    if len(low_swords) == 1 and new_sids[0] != current_slot1_sid:
        set_sword(client, DUNGEON_PARTY_NO, 1, new_sids[0])
        api_sleep()
        logger.info("队伍3重排完成（单刀swap）")
        return

    # 多把刀需要重排：全队拆装
    if new_sids[0] != current_slot1_sid:
        set_sword(client, DUNGEON_PARTY_NO, 1, new_sids[0])
        api_sleep()

    # 重新获取最新槽位（setsword swap 后位置可能变化），移除槽2-6
    cur_data = client.get_conquest_data()
    cur_slots = get_party_slots(cur_data, DUNGEON_PARTY_NO)
    for order, sid in sorted(cur_slots.items()):
        if order == 1:
            continue
        remove_sword(client, DUNGEON_PARTY_NO, order, sid)
        api_sleep()

    # 按新顺序放置槽2-6（new_sids[0] 已在槽1）
    for i, sid in enumerate(new_sids[1:], start=1):
        set_sword(client, DUNGEON_PARTY_NO, orders[i], sid)
        api_sleep()

    logger.info("队伍3重排完成")


# ── 单层战斗 ────────────────────────────────────────────────────

def _check_heavy_injury(getpartyinfo_resp: dict) -> bool:
    """检查 getpartyinfo 响应中是否有重伤刀，阈值与 repair.is_heavily_injured 保持一致"""
    for str_sid, sword in getpartyinfo_resp.get("sword", {}).items():
        if is_heavily_injured(sword):
            hp = sword.get("hp", 0)
            hp_max = sword.get("hp_max", 1)
            logger.warning(
                f"  重伤检测：serial_id={str_sid} "
                f"HP={hp}/{hp_max} ({hp * 100 // hp_max}%)"
            )
            return True
    return False


def _run_dungeon_floor(
    client: ToukenClient, layer_id: int, is_continuation: bool = False
) -> tuple[dict, bool, list[int]]:
    """
    执行单层地下城：
      eventsally(layer_id) → [eventforward + battle] × N → resetsallystatus → getpartyinfo
      → [updateautoplayflaginsally（仅重伤时）]

    is_continuation=True 对应行軍按钮（非首层），需额外传 is_continuation=1。
    返回 (eventsally_resp, has_heavy_injury, low_fatigue_sids)。
    """
    logger.info(f"  进入第 {layer_id} 层...")

    # 1. 进层（行軍时需 is_continuation=1）
    extra: dict = {
        "event_id":       DUNGEON_EVENT_ID,
        "event_field_id": DUNGEON_EVENT_FIELD_ID,
        "party_no":       DUNGEON_PARTY_NO,
        "item_id":        0,
        "event_layer_id": layer_id,
    }
    if is_continuation:
        extra["is_continuation"] = 1
    eventsally_resp = client._post("sally/eventsally", extra=extra)
    battle_sleep()

    # eventsally 失败（status≠0）→ 跳过战斗循环直接结算，清理残留会话
    eventsally_status = eventsally_resp.get("status", 0)
    if eventsally_status != 0:
        logger.warning(
            f"  eventsally status={eventsally_status}，进层失败，"
            f"直接结算清理残留会话..."
        )
        client._post("sally/resetsallystatus")
        api_sleep()

        # eventsally 失败时触发全队重伤检查+治疗。
        # 重伤阈值为 HEAVY_INJURY_RATIO（≤30%），对应游戏实测重伤标准。
        logger.warning("  eventsally 失败，触发全队重伤检查...")
        client._post("sally/updateautoplayflaginsally", extra={"type": 3})
        api_sleep()
        client._post("party/list")
        api_sleep()
        run_all_repairs(client)

        # 气力检查仍需 getpartyinfo
        getpartyinfo_resp = client._post("party/getpartyinfo", extra={"party_no": DUNGEON_PARTY_NO})
        api_sleep()
        has_heavy_injury = _check_heavy_injury(getpartyinfo_resp)
        low_fatigue_sids: list[int] = []
        critical_fatigue_sids: list[int] = []
        for str_sid, sword_data in getpartyinfo_resp.get("sword", {}).items():
            fat = sword_data.get("fatigue", 100)
            sid = int(str_sid)
            if fat < DUNGEON_FATIGUE_CRITICAL:
                critical_fatigue_sids.append(sid)
                low_fatigue_sids.append(sid)
            elif fat < DUNGEON_FATIGUE_THRESHOLD:
                low_fatigue_sids.append(sid)
        return eventsally_resp, has_heavy_injury, low_fatigue_sids, critical_fatigue_sids

    # 2. 战斗循环（直到 is_finish=true）
    battle_no = 0
    node_no = 0
    captain_injured = False  # 队长重伤导致 break
    while True:
        forward_resp = client._post("sally/eventforward", extra={"direction": 0})
        battle_sleep()

        forward_status = forward_resp.get("status", 0)
        is_finish      = forward_resp.get("is_finish", False)
        item_effect    = forward_resp.get("item_effect", 0)
        node_no += 1

        # 队长重伤时服务器返回 status≠0，无法继续出阵
        if forward_status != 0:
            logger.warning(
                f"    节点 {node_no}：eventforward status={forward_status}，"
                f"停止出阵（队长重伤）"
            )
            captain_injured = True
            break

        # item_effect != 0 为物资/事件节点，无需战斗
        if item_effect != 0:
            logger.debug(f"    节点 {node_no}：物资节点（item_effect={item_effect}），跳过战斗")
        else:
            # 游戏在 eventforward 响应中直接给出推荐阵型
            formation_id = (
                forward_resp.get("formation_id")
                or (forward_resp.get("scout") or {}).get("formation_id")
                or DUNGEON_DEFAULT_FORMATION
            )
            battle_no += 1
            logger.debug(f"    节点 {node_no}：战斗 {battle_no}（formation={formation_id}）is_finish={is_finish}")
            client._post("battle/battle", extra={"formation_id": formation_id})
            battle_sleep()

            # 道中重伤检查：非最终节点战斗后，有重伤则停止，不进入下一节点
            # 注意：conquest 无 hp/hp_max 字段，必须用 getpartyinfo 才能检测到重伤
            if not is_finish:
                pi = client._post("party/getpartyinfo", extra={"party_no": DUNGEON_PARTY_NO})
                api_sleep()
                if _check_heavy_injury(pi):
                    injured_sids = [
                        str(sid) for sid, sd in pi.get("sword", {}).items()
                        if is_heavily_injured(sd)
                    ]
                    logger.warning(
                        f"    节点 {node_no} 后检测到道中重伤（serial_id={injured_sids}），停止出阵"
                    )
                    break

        if is_finish:
            logger.info(f"  第 {layer_id} 层完成（{battle_no} 场战斗，共 {node_no} 个节点）")
            break

    # 3. 结算（resp 含新 token，由 client 自动更新）
    client._post("sally/resetsallystatus")
    api_sleep()

    # 4. 刷新队伍状态，同时检查重伤
    getpartyinfo_resp = client._post("party/getpartyinfo", extra={"party_no": DUNGEON_PARTY_NO})
    api_sleep()

    # 5. 若有重伤，发送确认并在 HP 数据准确窗口（resetsallystatus 后、sally 前）立即治疗
    # 队长重伤时 getpartyinfo HP 数据可能不准，直接强制标记
    has_heavy_injury = captain_injured or _check_heavy_injury(getpartyinfo_resp)
    if has_heavy_injury:
        logger.warning(f"  第 {layer_id} 层检测到重伤，发送确认并立即治疗（HP 准确窗口）...")
        client._post("sally/updateautoplayflaginsally", extra={"type": 3})
        api_sleep()
        client._post("party/list")
        api_sleep()
        # API 返回格式：sword[serial_id_str] = {...}，值中无 serial_id 字段，需从键取
        partylist_swords: dict[int, dict] = {}
        for str_sid, sd in getpartyinfo_resp.get("sword", {}).items():
            try:
                partylist_swords[int(str_sid)] = sd
            except (ValueError, TypeError):
                continue
        run_all_repairs(client, partylist_sword_data=partylist_swords)

    # 6. 检查气力：收集低于阈值的刀 serial_id 列表
    low_fatigue_sids: list[int] = []
    critical_fatigue_sids: list[int] = []
    for str_sid, sword_data in getpartyinfo_resp.get("sword", {}).items():
        fat = sword_data.get("fatigue", 100)
        sid = int(str_sid)
        if fat < DUNGEON_FATIGUE_CRITICAL:
            critical_fatigue_sids.append(sid)
            low_fatigue_sids.append(sid)
        elif fat < DUNGEON_FATIGUE_THRESHOLD:
            low_fatigue_sids.append(sid)
    if low_fatigue_sids:
        logger.info(
            f"  第 {layer_id} 层结束，低气力刀（{len(low_fatigue_sids)}把）：{low_fatigue_sids}"
            + (f"，其中极低气力：{critical_fatigue_sids}" if critical_fatigue_sids else "")
        )

    return eventsally_resp, has_heavy_injury, low_fatigue_sids, critical_fatigue_sids


# ── 单层循环入口 ─────────────────────────────────────────────────

def run_dungeon_floor_loop(
    client: ToukenClient,
    layer_id: int,
) -> None:
    """
    单层循环模式：在同一楼层无限循环，按 Ctrl+C 停止。

    继续调查按钮对应：getpartyinfo 之后，再次调用 eventsally(same_layer_id, is_continuation=1)。
    _run_dungeon_floor 已封装完整单层流程，循环调用即可：
      - 第1次（或归城后首次）：is_continuation=False
      - 后续各次：is_continuation=True
    每 DUNGEON_FLOORS_PER_CYCLE 次归城一次调整气力排序。
    """
    logger.info(
        f"地下城单层循环开始（第 {layer_id} 层，队伍{DUNGEON_PARTY_NO}，按 Ctrl+C 停止）"
    )

    # 进入活动页，预先排列气力
    client._post("sally")
    api_sleep()
    _sort_fatigued_swords_to_front(client)

    floors_left_in_cycle = DUNGEON_FLOORS_PER_CYCLE
    run_no = 0
    consecutive_eventsally_failures = 0
    MAX_EVENTSALLY_FAILURES = 2

    while True:
        run_no += 1

        if floors_left_in_cycle == 0:
            # 归城，重新排列气力，休息，再进入
            logger.info(f"  归城，调整气力排序，休息 {DUNGEON_REST_SECONDS}s...")
            client._post("sally")
            api_sleep()
            _sort_fatigued_swords_to_front(client)
            run_repair_check(client, DUNGEON_PARTY_NO)
            quick_expedition_check(client)
            time.sleep(DUNGEON_REST_SECONDS)
            logger.info("  休息结束，继续新一轮...")
            floors_left_in_cycle = DUNGEON_FLOORS_PER_CYCLE
            consecutive_eventsally_failures = 0

        # 归城后首次或第1次：is_continuation=False；其余：is_continuation=True
        is_cont = floors_left_in_cycle < DUNGEON_FLOORS_PER_CYCLE
        logger.info(f"[单层循环 第{run_no}次] 第 {layer_id} 层")
        resp, has_injury, low_fatigue_sids, critical_fatigue_sids = _run_dungeon_floor(client, layer_id, is_continuation=is_cont)

        # eventsally 失败处理（status≠0 表示未成功进层）
        if resp.get("status", 0) != 0:
            # 下次强制 is_continuation=False（本次未进层，无行軍状态）
            floors_left_in_cycle = DUNGEON_FLOORS_PER_CYCLE
            if has_injury:
                # 层内已完成治疗（eventsally 失败路径），归城重排后重试
                consecutive_eventsally_failures = 0
                logger.info("  eventsally 失败但重伤已处理，归城重排后重试...")
                client._post("sally")
                api_sleep()
                _sort_fatigued_swords_to_front(client)
                continue
            consecutive_eventsally_failures += 1
            if consecutive_eventsally_failures >= MAX_EVENTSALLY_FAILURES:
                logger.error(
                    f"eventsally 连续 {MAX_EVENTSALLY_FAILURES} 次失败且无重伤，"
                    "疑似已达今日上限或活动未开放，停止循环"
                )
                return
            logger.warning(
                f"  eventsally 失败（无重伤），等待 30s 重试"
                f"（{consecutive_eventsally_failures}/{MAX_EVENTSALLY_FAILURES}）..."
            )
            time.sleep(30)
            continue

        consecutive_eventsally_failures = 0

        if has_injury:
            logger.info("  重伤治疗已在层内完成，归城重排后继续...")
            client._post("sally")
            api_sleep()
            _sort_fatigued_swords_to_front(client)
            floors_left_in_cycle = DUNGEON_FLOORS_PER_CYCLE
            continue

        if len(low_fatigue_sids) >= 3 or critical_fatigue_sids:
            logger.info(
                f"  触发1-1气力恢复：低气力刀{len(low_fatigue_sids)}把"
                + (f"，极低气力（<{DUNGEON_FATIGUE_CRITICAL}）：{critical_fatigue_sids}" if critical_fatigue_sids else "")
            )
            client._post("sally")
            api_sleep()
            recover_sword_fatigue(client, DUNGEON_PARTY_NO, low_fatigue_sids)
            _sort_fatigued_swords_to_front(client)
            floors_left_in_cycle = DUNGEON_FLOORS_PER_CYCLE
            continue

        if low_fatigue_sids:
            logger.info("  检测到低气力，归城重排后继续...")
            client._post("sally")
            api_sleep()
            _sort_fatigued_swords_to_front(client)
            floors_left_in_cycle = DUNGEON_FLOORS_PER_CYCLE
            continue

        floors_left_in_cycle -= 1


# ── 爬楼主入口 ────────────────────────────────────────────────────

def run_dungeon_climb(client: ToukenClient, start_layer: int | None = None) -> None:
    """
    自动从当前层爬到 DUNGEON_TARGET_LAYER（含）。

    start_layer: 当前所在层（手动传入）。若不传则从终端 input() 获取。
    每打完 DUNGEON_FLOORS_PER_CYCLE 层归城一次（sally），
    在出阵地图重新排列低气力刀后继续下一轮。
    全部完成后归城并检查队伍3重伤。
    """
    if start_layer is None:
        start_layer = int(input("请输入当前地下城层数：").strip())

    logger.info(f"地下城爬楼开始（当前第 {start_layer} 层 → 目标第 {DUNGEON_TARGET_LAYER} 层，队伍{DUNGEON_PARTY_NO}）")

    if start_layer > DUNGEON_TARGET_LAYER:
        logger.info("已达到目标层数，无需爬楼")
        return

    # 进入活动页，排列低气力刀
    client._post("sally")
    api_sleep()
    _sort_fatigued_swords_to_front(client)

    floors_left_in_cycle = DUNGEON_FLOORS_PER_CYCLE
    current_layer = start_layer
    consecutive_eventsally_failures = 0
    MAX_EVENTSALLY_FAILURES = 2

    while current_layer <= DUNGEON_TARGET_LAYER:
        if floors_left_in_cycle == 0:
            # 归城，开始新周期
            logger.info("  归城...")
            client._post("sally")
            api_sleep()
            _sort_fatigued_swords_to_front(client)
            floors_left_in_cycle = DUNGEON_FLOORS_PER_CYCLE
            consecutive_eventsally_failures = 0

        # 新周期第一层不带 is_continuation；行軍延续时带
        is_cont = (floors_left_in_cycle < DUNGEON_FLOORS_PER_CYCLE)
        resp, has_injury, low_fatigue_sids, critical_fatigue_sids = _run_dungeon_floor(client, current_layer, is_continuation=is_cont)

        # eventsally 失败：不推进层数
        if resp.get("status", 0) != 0:
            floors_left_in_cycle = DUNGEON_FLOORS_PER_CYCLE  # 下次 is_continuation=False
            if has_injury:
                consecutive_eventsally_failures = 0
                logger.info("  eventsally 失败但重伤已处理，归城重排后重试同层...")
                client._post("sally")
                api_sleep()
                _sort_fatigued_swords_to_front(client)
                continue
            consecutive_eventsally_failures += 1
            if consecutive_eventsally_failures >= MAX_EVENTSALLY_FAILURES:
                logger.error(
                    f"eventsally 连续 {MAX_EVENTSALLY_FAILURES} 次失败且无重伤，"
                    "疑似已达今日上限或活动未开放，停止爬楼"
                )
                return
            logger.warning(
                f"  eventsally 失败（无重伤），等待 30s 重试同层"
                f"（{consecutive_eventsally_failures}/{MAX_EVENTSALLY_FAILURES}）..."
            )
            time.sleep(30)
            continue

        consecutive_eventsally_failures = 0

        if has_injury:
            logger.info("  重伤治疗已在层内完成，归城重排后继续...")
            client._post("sally")
            api_sleep()
            _sort_fatigued_swords_to_front(client)
            floors_left_in_cycle = DUNGEON_FLOORS_PER_CYCLE

        if len(low_fatigue_sids) >= 3 or critical_fatigue_sids:
            logger.info(
                f"  触发1-1气力恢复：低气力刀{len(low_fatigue_sids)}把"
                + (f"，极低气力（<{DUNGEON_FATIGUE_CRITICAL}）：{critical_fatigue_sids}" if critical_fatigue_sids else "")
            )
            client._post("sally")
            api_sleep()
            recover_sword_fatigue(client, DUNGEON_PARTY_NO, low_fatigue_sids)
            _sort_fatigued_swords_to_front(client)
            floors_left_in_cycle = DUNGEON_FLOORS_PER_CYCLE

        confirmed = resp.get("select_event_layer_num", current_layer)
        left      = DUNGEON_TARGET_LAYER - confirmed
        logger.info(f"  层 {confirmed} 完成，还剩 {left} 层")

        floors_left_in_cycle -= 1
        current_layer        += 1

    # 全部完成，归城并检查重伤
    logger.info("地下城全部层打完，归城...")
    client._post("sally")
    api_sleep()
    run_repair_check(client, DUNGEON_PARTY_NO)
    logger.info("地下城全部结束")

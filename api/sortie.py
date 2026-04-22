"""
出阵模块 —— 地图循环刷资源

变种：
  run_sortie_4_4_loop  —— 4-4，队伍2，每15次检查依赖札决定休息时长
  run_sortie_4_3_loop  —— 4-3，队伍4，每15次固定休息，不检查依赖札

单次出阵流程：
  1. 气力检查：非队长刀气力 < FATIGUE_LOW_THRESHOLD → 换至队长位(slot 1)
  2. 重伤检查：有重伤则先触发全队治疗
  3. sally/sally（选图，无 party_no）  → HTML
  4. sally/sally（选队，含 party_no）  → JSON
  5. sally/startup                     → JSON，history 长度 ÷ 2 = 本次战斗场数
  6. [sally/forward + battle/battle] × N
  7. 回本丸同步状态 + 快速远征检查

端点（已通过抓包确认）：
  user/item  — 查询道具，响应顶层 bill = 依赖札数量
  episode_id=4, field_id=4, party_no=2 — 4-4 地图参数
  episode_id=4, field_id=3, party_no=4 — 4-3 地图参数
"""

import random
import time
from loguru import logger

from .client import ToukenClient
from .battle import battle_sleep, FORMATION_ID, set_sword, remove_sword, get_party_slots, recover_sword_fatigue
import datetime

# 阵型克制表：enemy_formation → counter_formation
# 鋒矢(1)→逆行(3), 横隊(2)→鶴翼(5), 逆行(3)→方圓(6),
# 鱼鳞(4)→鋒矢(1), 鶴翼(5)→鱼鳞(4), 方圓(6)→横隊(2)
COUNTER_FORMATION: dict[int, int] = {
    1: 3, 2: 5, 3: 6, 4: 1, 5: 4, 6: 2,
}
from .repair import run_repair_check, find_hakusan_serial_id, HAKUSAN_PARTY_NO, is_heavily_injured
from .expedition import quick_expedition_check

# 出阵队伍初始编成（循环启动时记录，治疗后验证用）
_initial_party_members: dict[int, dict[int, int]] = {}  # {party_no: {order: serial_id}}

# 限时锻造检查（可选，启用时在休息间隙自动处理锻造槽）
_forge_event_enabled: bool = False


def snapshot_party(client: ToukenClient, party_no: int) -> None:
    """循环启动时调用，记录队伍初始编成（order → serial_id）"""
    global _initial_party_members
    conquest = client.get_conquest_data()
    slots = get_party_slots(conquest, party_no)
    _initial_party_members[party_no] = dict(slots)
    logger.debug(f"部隊{party_no} 初始编成：{len(slots)} 人 — {slots}")


def verify_party_after_repair(client: ToukenClient, party_no: int) -> None:
    """治疗后验证队伍编成，缺人则补回+自动装备，补不回才停止"""
    expected = _initial_party_members.get(party_no)
    if not expected:
        return

    conquest = client.get_conquest_data()
    current_slots = get_party_slots(conquest, party_no)
    current_sids = set(current_slots.values())
    expected_sids = set(expected.values())

    missing = expected_sids - current_sids
    if not missing:
        return

    logger.opt(colors=True).warning(
        f"<red>部隊{party_no} 治疗后丢失 {len(missing)} 人：{missing}</red>"
    )

    # 尝试补回
    used_orders = set(current_slots.keys())
    for sid in missing:
        # 找这把刀原来在哪个槽
        orig_order = next((o for o, s in expected.items() if s == sid), None)
        # 如果原槽空着就放原槽，否则找空槽
        if orig_order and orig_order not in used_orders:
            target_order = orig_order
        else:
            target_order = next((i for i in range(2, 7) if i not in used_orders), None)

        if target_order is None:
            logger.error(f"  serial_id={sid} 无空槽可补回！")
            continue

        logger.info(f"  补回 serial_id={sid} 至 部隊{party_no} 槽{target_order}...")
        set_sword(client, party_no, target_order, sid)
        battle_sleep()
        used_orders.add(target_order)

        # 检查刀装，没有则自动装备
        pi = client._post("party/getpartyinfo", extra={"party_no": party_no})
        battle_sleep()
        sword_data = pi.get("sword", {}).get(str(sid), {})
        has_equip = any(sword_data.get(f"equip_serial_id{i}") for i in range(1, 5))
        if not has_equip:
            logger.info(f"  serial_id={sid} 无刀装，自动装备...")
            client._post("equip/autosetequip", extra={
                "sword_serial_id": sid,
                "is_event": 0,
                "is_firstattack": 1,
            })
            battle_sleep()

    # 补回后再验证一次
    conquest2 = client.get_conquest_data()
    final_slots = get_party_slots(conquest2, party_no)
    final_sids = set(final_slots.values())
    still_missing = expected_sids - final_sids
    if still_missing:
        logger.opt(colors=True).error(
            f"<red>部隊{party_no} 补回失败，仍缺 {still_missing}，停止运行</red>"
        )
        raise RuntimeError(f"部隊{party_no} 编成修复失败，缺 {still_missing}")
    else:
        logger.opt(colors=True).info(f"<green>部隊{party_no} 编成已修复（{len(final_slots)} 人）</green>")


def enable_forge_event():
    """启用限时锻造自动检查"""
    global _forge_event_enabled
    _forge_event_enabled = True
    logger.info("限时锻造检查已启用（休息间隙自动领取+再开）")


def _check_forge_if_enabled(client: ToukenClient) -> None:
    """休息间隙调用：如果启用了锻造检查，执行一轮"""
    global _forge_event_enabled
    if not _forge_event_enabled:
        return
    try:
        from .forge_event import run_forge_check
        logger.debug("锻造检查...")
        found_new = run_forge_check(client)
        if found_new:
            _forge_event_enabled = False
            logger.info("发现新刀，已自动关闭锻造检查")
    except Exception as e:
        logger.warning(f"锻造检查异常：{e}")


def _get_resources(client: ToukenClient) -> dict:
    """从 home/index 获取当前资源"""
    state = client._post("home/index")
    return state.get("resource", {})


_last_good_res: dict = {}  # 最近一次成功获取的资源快照


def _log_resource_diff(start_res: dict, end_res: dict) -> None:
    """打印资源变化汇总（end_res 无效时用上次快照）"""
    global _last_good_res
    if not end_res or "charcoal" not in end_res:
        if _last_good_res and "charcoal" in _last_good_res:
            logger.info("  （token 断裂，使用上次快照）")
            end_res = _last_good_res
        else:
            logger.debug("  资源汇总跳过（无可用数据）")
            return
    else:
        _last_good_res = end_res
    items = [
        ("charcoal", "木炭"), ("steel", "玉钢"),
        ("coolant", "冷却材"), ("file", "砥石"),
    ]
    parts = []
    for key, name in items:
        current = end_res.get(key, 0)
        diff = current - start_res.get(key, 0)
        sign = "+" if diff >= 0 else ""
        parts.append(f"{name}:{sign}{diff}({current})")
    logger.info(f"  资源变化：{'  '.join(parts)}")

# ── 共用配置 ──────────────────────────────────────────────────
REST_INTERVAL_MIN:          int = 20  # 休息间隔最小次数
REST_INTERVAL_MAX:          int = 50  # 休息间隔最大次数
FATIGUE_LOW_THRESHOLD:      int = 70  # 非队长气力低于此值则换至队长位
FATIGUE_CRITICAL_THRESHOLD: int = 40  # 低于此值即使只有1把也触发1-1恢复

# ── 4-2 循环配置（主砥石）─────────────────────────────────────
SORTIE_42_PARTY_NO:              int = 3
SORTIE_42_EPISODE_ID:            int = 4
SORTIE_42_FIELD_ID:              int = 2   # 4-2
SORTIE_42_CHECK_INTERVAL:        int = 15
SORTIE_42_REST_SECONDS:          int = 5

# ── 4-3 循环配置（主冷却材）──────────────────────────────────
SORTIE_43_PARTY_NO:              int = 3
SORTIE_43_EPISODE_ID:            int = 4
SORTIE_43_FIELD_ID:              int = 3   # 4-3
SORTIE_43_CHECK_INTERVAL:        int = 15
SORTIE_43_REST_SECONDS:          int = 5

# ── 4-4 循环配置（主委托符）──────────────────────────────────
SORTIE_44_PARTY_NO:              int = 3
SORTIE_44_EPISODE_ID:            int = 4
SORTIE_44_FIELD_ID:              int = 4   # 4-4
SORTIE_44_CHECK_INTERVAL:        int = 15
YORAIFUDA_REST_LOW_THRESHOLD:    int = 10
YORAIFUDA_REST_SHORT_SECONDS:    int = 5
YORAIFUDA_REST_LONG_SECONDS:     int = 10

# ── 5-2 循环配置（主木炭）────────────────────────────────────
SORTIE_52_PARTY_NO:              int = 3
SORTIE_52_EPISODE_ID:            int = 5
SORTIE_52_FIELD_ID:              int = 2   # 5-2
SORTIE_52_CHECK_INTERVAL:        int = 15
SORTIE_52_REST_SECONDS:          int = 5

# ── 6-1 循环配置（主砥石，高速枪，马匹减半）──────────────────
SORTIE_61_PARTY_NO:              int = 3
SORTIE_61_EPISODE_ID:            int = 6
SORTIE_61_FIELD_ID:              int = 1   # 6-1
SORTIE_61_CHECK_INTERVAL:        int = 15
SORTIE_61_REST_SECONDS:          int = 5

# ── 7-3 循环配置（队伍3，固定休息，不检查依赖札）────────────
SORTIE_73_PARTY_NO:              int = 3
SORTIE_73_EPISODE_ID:            int = 7
SORTIE_73_FIELD_ID:              int = 3   # 7-3
SORTIE_73_CHECK_INTERVAL:        int = 15
SORTIE_73_REST_SECONDS:          int = 5   # 固定休息时长

# ── 7-4 循环配置（队伍3，固定休息）──────────────────────────
SORTIE_74_PARTY_NO:              int = 3
SORTIE_74_EPISODE_ID:            int = 7
SORTIE_74_FIELD_ID:              int = 4   # 7-4
SORTIE_74_CHECK_INTERVAL:        int = 15
SORTIE_74_REST_SECONDS:          int = 5


# ── 白山移除（4-4 出阵前调用）────────────────────────────────

def remove_hakusan_for_sortie(client: ToukenClient) -> None:
    """
    4-4 循环前将白山吉光从 部隊2 移除，使末位成员升为新队长（槽1）。

    步骤：
      1. 找出 部隊2 中序号最大的非槽1成员（末位）
      2. setsword(slot=末位, sid=白山) → 白山换至末槽，末位成员自动换到槽1成为新队长
      3. removesword(slot=末位, sid=白山) → 移除白山
    """
    conquest_data = client.get_conquest_data()
    hakusan_sid = find_hakusan_serial_id(conquest_data)
    if hakusan_sid is None:
        logger.warning("找不到白山吉光，跳过移除")
        return

    slots = get_party_slots(conquest_data, HAKUSAN_PARTY_NO)
    if slots.get(1) != hakusan_sid:
        logger.debug(f"部隊{HAKUSAN_PARTY_NO} 槽1不是白山（sid={slots.get(1)}），跳过")
        return

    last_order = max((o for o in slots if o > 1), default=None)
    if last_order is None:
        logger.warning("部隊2只有白山一人，无其他成员可互换，跳过移除")
        return

    last_sid = slots[last_order]
    logger.info(
        f"移除白山吉光：槽1↔槽{last_order}（serial_id={last_sid}升为新队长）..."
    )
    set_sword(client, HAKUSAN_PARTY_NO, last_order, hakusan_sid)
    battle_sleep()
    remove_sword(client, HAKUSAN_PARTY_NO, last_order, hakusan_sid)
    battle_sleep()
    logger.info(f"  白山已移除，部隊{HAKUSAN_PARTY_NO} 新队长 serial_id={last_sid}")


# ── 道具查询 ──────────────────────────────────────────────────

def get_bill(client: ToukenClient) -> int:
    """查询当前依赖札数量（user/item 响应顶层 bill 字段）"""
    resp = client._post("user/item")
    bill = resp.get("bill", 0)
    logger.debug(f"依赖札当前数量：{bill}")
    return bill


# ── 气力换位 ──────────────────────────────────────────────────

def adjust_captain_for_fatigue(client: ToukenClient, party_no: int) -> None:
    """
    检查指定队伍非队长刀气力，若有低于 FATIGUE_LOW_THRESHOLD 的，
    将气力最低的那把换至队长位（slot 1）。
    使用 setsword swap，不调用 removesword（槽1队长无法被移除）。
    """
    conquest_data = client.get_conquest_data()
    slots = get_party_slots(conquest_data, party_no)
    if not slots:
        return

    all_swords = conquest_data.get("sword", {})

    candidate_order: int | None = None
    candidate_sid:   int | None = None
    lowest_fatigue = FATIGUE_LOW_THRESHOLD

    for order, sid in slots.items():
        if order == 1:
            continue
        fat = all_swords.get(str(sid), {}).get("fatigue", FATIGUE_LOW_THRESHOLD)
        if fat < lowest_fatigue:
            lowest_fatigue = fat
            candidate_order = order
            candidate_sid = sid

    if candidate_sid is None:
        logger.debug(f"队{party_no}气力检查：全员正常，无需换位")
        return

    logger.info(
        f"队{party_no} serial_id={candidate_sid} 气力={lowest_fatigue}<{FATIGUE_LOW_THRESHOLD}，"
        f"换至队长位（槽1）"
    )
    set_sword(client, party_no, 1, candidate_sid)
    battle_sleep()


# ── 气力恢复检查（出阵后调用）────────────────────────────────

def _check_and_recover_fatigue(client: ToukenClient, party_no: int) -> None:
    """
    出阵结束后检查队伍气力。
    低于 FATIGUE_LOW_THRESHOLD 的刀达 3 把及以上，或任意刀低于 FATIGUE_CRITICAL_THRESHOLD，
    逐刀跑 1-1 补满气力。
    """
    partyinfo_resp = client._post("party/getpartyinfo", extra={"party_no": party_no})
    low_sids: list[int] = []
    critical_sids: list[int] = []
    for str_sid, sword_data in partyinfo_resp.get("sword", {}).items():
        fat = sword_data.get("fatigue", 100)
        sid = int(str_sid)
        if fat < FATIGUE_CRITICAL_THRESHOLD:
            critical_sids.append(sid)
            low_sids.append(sid)
        elif fat < FATIGUE_LOW_THRESHOLD:
            low_sids.append(sid)
    if len(low_sids) >= 3 or critical_sids:
        logger.info(
            f"队{party_no} 触发1-1气力恢复：低气力刀{len(low_sids)}把"
            + (f"，极低气力（<{FATIGUE_CRITICAL_THRESHOLD}）：{critical_sids}" if critical_sids else "")
        )
        recover_sword_fatigue(client, party_no, low_sids)


# ── 单次出阵 ─────────────────────────────────────────────────

def _run_single_sortie(
    client: ToukenClient,
    party_no: int,
    episode_id: int,
    field_id: int,
    skip_last_battle: bool = False,
) -> None:
    """
    执行一次出阵（不含气力/重伤检查）。
    skip_last_battle=True 时，最终节点（is_finish=True）不战斗直接归城，减少刀装消耗。
    """
    client._post("sally")
    battle_sleep()

    client._post("sally/sally", extra={
        "episode_id": episode_id,
        "field_id":   field_id,
        "party_no":   party_no,
    })
    battle_sleep()

    startup_resp = client._post("sally/startup")
    battle_sleep()

    battle_no = 0
    node_no = 0
    while True:
        forward_resp = client._post("sally/forward", extra={"direction": 0})
        battle_sleep()

        forward_status = forward_resp.get("status", 0)
        is_finish      = forward_resp.get("is_finish", False)
        item_effect    = forward_resp.get("item_effect", 0)
        node_no += 1

        # 队长重伤时服务器返回 status≠0，无法继续出阵
        if forward_status != 0:
            logger.warning(
                f"  节点 {node_no}：forward status={forward_status}，"
                f"停止出阵（队长重伤）"
            )
            break

        # 物资节点，无需战斗
        if item_effect != 0:
            logger.debug(f"  节点 {node_no}：物资节点（item_effect={item_effect}），跳过战斗")
        elif skip_last_battle and is_finish:
            logger.info(f"  节点 {node_no}：最终节点，跳过战斗直接归城（skip_last_battle）")
            break
        else:
            enemy_formation = (forward_resp.get("scout") or {}).get("formation_id", 0)
            formation_id = COUNTER_FORMATION.get(enemy_formation, FORMATION_ID)
            battle_no += 1
            logger.debug(f"  节点 {node_no}：战斗 {battle_no}（formation={formation_id}）is_finish={is_finish}")
            client._post("battle/battle", extra={"formation_id": formation_id})
            battle_sleep()

            # 道中重伤检查：非最终节点战斗后，有重伤则立刻停止
            if not is_finish:
                pi = client._post("party/getpartyinfo", extra={"party_no": party_no})
                battle_sleep()
                injured = [str_sid for str_sid, sd in pi.get("sword", {}).items() if is_heavily_injured(sd)]
                if injured:
                    logger.opt(colors=True).warning(
                        f"<red>  节点 {node_no} 后检测到道中重伤（serial_id={injured}），停止出阵</red>"
                    )
                    break

        if is_finish:
            logger.info(f"  出阵完成（{battle_no} 场战斗，共 {node_no} 个节点）")
            break

    client.get_game_state()
    logger.info("  出阵结束，已回本丸")


# ── 4-4 循环（队伍2，检查依赖札）────────────────────────────

def run_sortie_4_4_loop(client: ToukenClient) -> None:
    """
    队伍2 无限循环刷 4-4。
    每 SORTIE_44_CHECK_INTERVAL 次检查依赖札增量：
      < 10个 → 休息 5s；>= 10个 → 休息 10s。
    按 Ctrl+C 手动停止。
    白山吉光现驻守 部隊1，与本队（部隊2）互不干扰，无需移除。
    """
    start_res = _get_resources(client)
    initial_bill = get_bill(client)
    last_check_bill = initial_bill
    total_runs = 0
    next_rest = random.randint(REST_INTERVAL_MIN, REST_INTERVAL_MAX)
    start_time = datetime.datetime.now()
    logger.info(
        f"4-4 循环开始（队伍{SORTIE_44_PARTY_NO}），依赖札初始：{initial_bill}，"
        f"每 {REST_INTERVAL_MIN}-{REST_INTERVAL_MAX} 次随机休息"
    )

    LIGHT_CHECK_INTERVAL = 5

    try:
        while True:
            if total_runs % LIGHT_CHECK_INTERVAL == 0:
                adjust_captain_for_fatigue(client, SORTIE_44_PARTY_NO)
            run_repair_check(client, SORTIE_44_PARTY_NO)

            total_runs += 1
            logger.info(f"[第 {total_runs} 次] 出阵 4-4（队伍{SORTIE_44_PARTY_NO}）...")
            _run_single_sortie(client, SORTIE_44_PARTY_NO, SORTIE_44_EPISODE_ID, SORTIE_44_FIELD_ID)

            if total_runs % LIGHT_CHECK_INTERVAL == 0:
                _check_and_recover_fatigue(client, SORTIE_44_PARTY_NO)
                quick_expedition_check(client)

            if total_runs >= next_rest:
                current_bill = get_bill(client)
                gained = current_bill - last_check_bill
                rest = YORAIFUDA_REST_SHORT_SECONDS if gained < YORAIFUDA_REST_LOW_THRESHOLD else YORAIFUDA_REST_LONG_SECONDS
                logger.info(
                    f"[第 {total_runs} 次检查] 依赖札：{last_check_bill} → {current_bill}"
                    f"（+{gained}），休息 {rest}s..."
                )
                time.sleep(rest)
                _check_forge_if_enabled(client)
                _log_resource_diff(start_res, _get_resources(client))
                next_rest = total_runs + random.randint(REST_INTERVAL_MIN, REST_INTERVAL_MAX)
                logger.info("  休息结束，继续新一轮...")
                last_check_bill = current_bill
    except KeyboardInterrupt:
        elapsed = datetime.datetime.now() - start_time
        final_bill = get_bill(client) if total_runs > 0 else initial_bill
        logger.info(
            f"4-4 循环结束 — 共 {total_runs} 次，"
            f"耗时 {str(elapsed).split('.')[0]}，"
            f"依赖札 {initial_bill} → {final_bill}（+{final_bill - initial_bill}）"
        )
        try:
            _log_resource_diff(start_res, _get_resources(client))
        except Exception:
            pass
        raise


# ── 4-2 循环（主砥石）────────────────────────────────────────

def run_sortie_4_2_loop(client: ToukenClient) -> None:
    """队伍3 无限循环刷 4-2（主砥石）。"""
    start_res = _get_resources(client)
    snapshot_party(client, SORTIE_44_PARTY_NO)
    total_runs = 0
    next_rest = random.randint(REST_INTERVAL_MIN, REST_INTERVAL_MAX)
    start_time = datetime.datetime.now()
    logger.info(
        f"4-2 循环开始（队伍{SORTIE_42_PARTY_NO}，主砥石），"
        f"每 {REST_INTERVAL_MIN}-{REST_INTERVAL_MAX} 次随机休息"
    )
    LIGHT_CHECK_INTERVAL = 5

    try:
        while True:
            if total_runs % LIGHT_CHECK_INTERVAL == 0:
                adjust_captain_for_fatigue(client, SORTIE_42_PARTY_NO)
            run_repair_check(client, SORTIE_42_PARTY_NO)
            total_runs += 1
            logger.info(f"[第 {total_runs} 次] 出阵 4-2（队伍{SORTIE_42_PARTY_NO}）...")
            _run_single_sortie(client, SORTIE_42_PARTY_NO, SORTIE_42_EPISODE_ID, SORTIE_42_FIELD_ID)
            if total_runs % LIGHT_CHECK_INTERVAL == 0:
                _check_and_recover_fatigue(client, SORTIE_42_PARTY_NO)
                quick_expedition_check(client)
            if total_runs >= next_rest:
                logger.info(f"[第 {total_runs} 次] 休息 {SORTIE_42_REST_SECONDS}s...")
                time.sleep(SORTIE_42_REST_SECONDS)
                _check_forge_if_enabled(client)
                _log_resource_diff(start_res, _get_resources(client))
                next_rest = total_runs + random.randint(REST_INTERVAL_MIN, REST_INTERVAL_MAX)
                logger.info("  休息结束，继续新一轮...")
    except KeyboardInterrupt:
        elapsed = datetime.datetime.now() - start_time
        logger.info(f"4-2 循环结束 — 共 {total_runs} 次，耗时 {str(elapsed).split('.')[0]}")
        try:
            _log_resource_diff(start_res, _get_resources(client))
        except Exception:
            pass
        raise


# ── 4-3 循环（主冷却材）──────────────────────────────────────

def run_sortie_4_3_loop(client: ToukenClient) -> None:
    """
    队伍3 无限循环刷 4-3。
    每 SORTIE_43_CHECK_INTERVAL 次固定休息 SORTIE_43_REST_SECONDS 秒，不检查依赖札。
    按 Ctrl+C 手动停止。
    """
    start_res = _get_resources(client)
    snapshot_party(client, SORTIE_44_PARTY_NO)
    total_runs = 0
    next_rest = random.randint(REST_INTERVAL_MIN, REST_INTERVAL_MAX)
    start_time = datetime.datetime.now()
    logger.info(
        f"4-3 循环开始（队伍{SORTIE_43_PARTY_NO}），"
        f"每 {REST_INTERVAL_MIN}-{REST_INTERVAL_MAX} 次随机休息"
    )

    LIGHT_CHECK_INTERVAL = 5

    try:
        while True:
            if total_runs % LIGHT_CHECK_INTERVAL == 0:
                adjust_captain_for_fatigue(client, SORTIE_43_PARTY_NO)
            run_repair_check(client, SORTIE_43_PARTY_NO)

            total_runs += 1
            logger.info(f"[第 {total_runs} 次] 出阵 4-3（队伍{SORTIE_43_PARTY_NO}）...")
            _run_single_sortie(client, SORTIE_43_PARTY_NO, SORTIE_43_EPISODE_ID, SORTIE_43_FIELD_ID)

            if total_runs % LIGHT_CHECK_INTERVAL == 0:
                _check_and_recover_fatigue(client, SORTIE_43_PARTY_NO)
                quick_expedition_check(client)

            if total_runs >= next_rest:
                logger.info(f"[第 {total_runs} 次] 休息 {SORTIE_43_REST_SECONDS}s...")
                time.sleep(SORTIE_43_REST_SECONDS)
                _check_forge_if_enabled(client)
                _log_resource_diff(start_res, _get_resources(client))
                next_rest = total_runs + random.randint(REST_INTERVAL_MIN, REST_INTERVAL_MAX)
                logger.info("  休息结束，继续新一轮...")
    except KeyboardInterrupt:
        elapsed = datetime.datetime.now() - start_time
        logger.info(f"4-3 循环结束 — 共 {total_runs} 次，耗时 {str(elapsed).split('.')[0]}")
        try:
            _log_resource_diff(start_res, _get_resources(client))
        except Exception:
            pass
        raise


def run_sortie_7_3_loop(client: ToukenClient) -> None:
    """
    队伍3 无限循环刷 7-3。
    每 SORTIE_73_CHECK_INTERVAL 次固定休息 SORTIE_73_REST_SECONDS 秒，不检查依赖札。
    按 Ctrl+C 手动停止。
    """
    start_res = _get_resources(client)
    snapshot_party(client, SORTIE_44_PARTY_NO)
    total_runs = 0
    next_rest = random.randint(REST_INTERVAL_MIN, REST_INTERVAL_MAX)
    start_time = datetime.datetime.now()
    logger.info(
        f"7-3 循环开始（队伍{SORTIE_73_PARTY_NO}），"
        f"每 {REST_INTERVAL_MIN}-{REST_INTERVAL_MAX} 次随机休息"
    )

    LIGHT_CHECK_INTERVAL = 5

    try:
        while True:
            if total_runs % LIGHT_CHECK_INTERVAL == 0:
                adjust_captain_for_fatigue(client, SORTIE_73_PARTY_NO)
            run_repair_check(client, SORTIE_73_PARTY_NO)

            total_runs += 1
            logger.info(f"[第 {total_runs} 次] 出阵 7-3（队伍{SORTIE_73_PARTY_NO}）...")
            _run_single_sortie(client, SORTIE_73_PARTY_NO, SORTIE_73_EPISODE_ID, SORTIE_73_FIELD_ID, skip_last_battle=True)

            if total_runs % LIGHT_CHECK_INTERVAL == 0:
                _check_and_recover_fatigue(client, SORTIE_73_PARTY_NO)
                quick_expedition_check(client)

            if total_runs >= next_rest:
                logger.info(f"[第 {total_runs} 次] 休息 {SORTIE_73_REST_SECONDS}s...")
                time.sleep(SORTIE_73_REST_SECONDS)
                _check_forge_if_enabled(client)
                _log_resource_diff(start_res, _get_resources(client))
                next_rest = total_runs + random.randint(REST_INTERVAL_MIN, REST_INTERVAL_MAX)
                logger.info("  休息结束，继续新一轮...")
    except KeyboardInterrupt:
        elapsed = datetime.datetime.now() - start_time
        logger.info(f"7-3 循环结束 — 共 {total_runs} 次，耗时 {str(elapsed).split('.')[0]}")
        try:
            _log_resource_diff(start_res, _get_resources(client))
        except Exception:
            pass
        raise


def run_sortie_7_4_loop(client: ToukenClient) -> None:
    """队伍3 无限循环刷 7-4。"""
    start_res = _get_resources(client)
    snapshot_party(client, SORTIE_44_PARTY_NO)
    total_runs = 0
    next_rest = random.randint(REST_INTERVAL_MIN, REST_INTERVAL_MAX)
    start_time = datetime.datetime.now()
    logger.info(
        f"7-4 循环开始（队伍{SORTIE_74_PARTY_NO}），"
        f"每 {REST_INTERVAL_MIN}-{REST_INTERVAL_MAX} 次随机休息"
    )

    LIGHT_CHECK_INTERVAL = 5

    try:
        while True:
            if total_runs % LIGHT_CHECK_INTERVAL == 0:
                adjust_captain_for_fatigue(client, SORTIE_74_PARTY_NO)
            run_repair_check(client, SORTIE_74_PARTY_NO)

            total_runs += 1
            logger.info(f"[第 {total_runs} 次] 出阵 7-4（队伍{SORTIE_74_PARTY_NO}）...")
            _run_single_sortie(client, SORTIE_74_PARTY_NO, SORTIE_74_EPISODE_ID, SORTIE_74_FIELD_ID)

            if total_runs % LIGHT_CHECK_INTERVAL == 0:
                _check_and_recover_fatigue(client, SORTIE_74_PARTY_NO)
                quick_expedition_check(client)

            if total_runs >= next_rest:
                logger.info(f"[第 {total_runs} 次] 休息 {SORTIE_74_REST_SECONDS}s...")
                time.sleep(SORTIE_74_REST_SECONDS)
                _check_forge_if_enabled(client)
                _log_resource_diff(start_res, _get_resources(client))
                next_rest = total_runs + random.randint(REST_INTERVAL_MIN, REST_INTERVAL_MAX)
                logger.info("  休息结束，继续新一轮...")
    except KeyboardInterrupt:
        elapsed = datetime.datetime.now() - start_time
        logger.info(f"7-4 循环结束 — 共 {total_runs} 次，耗时 {str(elapsed).split('.')[0]}")
        try:
            _log_resource_diff(start_res, _get_resources(client))
        except Exception:
            pass
        raise


# ── 5-2 循环（主木炭）────────────────────────────────────────

def run_sortie_5_2_loop(client: ToukenClient) -> None:
    """队伍3 无限循环刷 5-2（主木炭）。"""
    start_res = _get_resources(client)
    snapshot_party(client, SORTIE_44_PARTY_NO)
    total_runs = 0
    next_rest = random.randint(REST_INTERVAL_MIN, REST_INTERVAL_MAX)
    start_time = datetime.datetime.now()
    logger.info(
        f"5-2 循环开始（队伍{SORTIE_52_PARTY_NO}，主木炭），"
        f"每 {REST_INTERVAL_MIN}-{REST_INTERVAL_MAX} 次随机休息"
    )
    LIGHT_CHECK_INTERVAL = 5  # 气力/远征轻量检查间隔

    try:
        while True:
            if total_runs % LIGHT_CHECK_INTERVAL == 0:
                adjust_captain_for_fatigue(client, SORTIE_52_PARTY_NO)
            run_repair_check(client, SORTIE_52_PARTY_NO)
            total_runs += 1
            logger.info(f"[第 {total_runs} 次] 出阵 5-2（队伍{SORTIE_52_PARTY_NO}）...")
            _run_single_sortie(client, SORTIE_52_PARTY_NO, SORTIE_52_EPISODE_ID, SORTIE_52_FIELD_ID)
            if total_runs % LIGHT_CHECK_INTERVAL == 0:
                _check_and_recover_fatigue(client, SORTIE_52_PARTY_NO)
                quick_expedition_check(client)
            if total_runs >= next_rest:
                logger.info(f"[第 {total_runs} 次] 休息 {SORTIE_52_REST_SECONDS}s...")
                time.sleep(SORTIE_52_REST_SECONDS)
                _check_forge_if_enabled(client)
                _log_resource_diff(start_res, _get_resources(client))
                next_rest = total_runs + random.randint(REST_INTERVAL_MIN, REST_INTERVAL_MAX)
                logger.info("  休息结束，继续新一轮...")
    except KeyboardInterrupt:
        elapsed = datetime.datetime.now() - start_time
        logger.info(f"5-2 循环结束 — 共 {total_runs} 次，耗时 {str(elapsed).split('.')[0]}")
        try:
            _log_resource_diff(start_res, _get_resources(client))
        except Exception:
            pass
        raise


# ── 6-1 循环（主砥石，高速枪，马匹减半，推荐极短速度>150）───

def run_sortie_6_1_loop(client: ToukenClient) -> None:
    """队伍3 无限循环刷 6-1（主砥石，高速枪，马匹属性减半）。"""
    start_res = _get_resources(client)
    snapshot_party(client, SORTIE_44_PARTY_NO)
    total_runs = 0
    next_rest = random.randint(REST_INTERVAL_MIN, REST_INTERVAL_MAX)
    start_time = datetime.datetime.now()
    logger.info(
        f"6-1 循环开始（队伍{SORTIE_61_PARTY_NO}，主砥石，高速枪/马匹减半），"
        f"每 {REST_INTERVAL_MIN}-{REST_INTERVAL_MAX} 次随机休息"
    )
    try:
        while True:
            adjust_captain_for_fatigue(client, SORTIE_61_PARTY_NO)
            run_repair_check(client, SORTIE_61_PARTY_NO)
            total_runs += 1
            logger.info(f"[第 {total_runs} 次] 出阵 6-1（队伍{SORTIE_61_PARTY_NO}）...")
            _run_single_sortie(client, SORTIE_61_PARTY_NO, SORTIE_61_EPISODE_ID, SORTIE_61_FIELD_ID)
            _check_and_recover_fatigue(client, SORTIE_61_PARTY_NO)
            quick_expedition_check(client)
            if total_runs >= next_rest:
                logger.info(f"[第 {total_runs} 次] 休息 {SORTIE_61_REST_SECONDS}s...")
                time.sleep(SORTIE_61_REST_SECONDS)
                _check_forge_if_enabled(client)
                _log_resource_diff(start_res, _get_resources(client))
                next_rest = total_runs + random.randint(REST_INTERVAL_MIN, REST_INTERVAL_MAX)
                logger.info("  休息结束，继续新一轮...")
    except KeyboardInterrupt:
        elapsed = datetime.datetime.now() - start_time
        logger.info(f"6-1 循环结束 — 共 {total_runs} 次，耗时 {str(elapsed).split('.')[0]}")
        try:
            _log_resource_diff(start_res, _get_resources(client))
        except Exception:
            pass
        raise

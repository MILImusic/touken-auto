"""
重伤治疗模块

架构：
  白山吉光（sword_id=164/165，支持極化与未極化）永久驻守 部隊1 槽1，作为固定治疗队长。
  部隊1 平时只放白山，槽2-6 空置。

流程：
  0. 检查白山吉光气力，若 < HAKUSAN_FATIGUE_MIN 则直接用部隊1刷 1-1 恢复（不改变编成）
  1. 快照 部隊1 槽2-6 原编成（通常为空）
  2. 找出所有重伤刀（hp/hp_max <= 30%）
  3. 清空 部隊1 槽2-6
  4. 逐一治疗：
     - 将重伤刀放入 部隊1 槽2
     - equip/removeall 卸装（远程刀装会清场导致白山无法治疗）
     - 循环刷 1-1，直到 HP > 30% 或超出最大轮次
     - 移除重伤刀，还原刀装
  5. 还原 部隊1 槽2-6 原编成

约束：
  - 大太刀/薙刀额外：卸装后基础速度（mobile-mobile_up）必须 < 54，否则 AOE 仍会清场
  - 部隊1 与 4-4（部隊2）/ 4-3（部隊4）/ 地下城（部隊3）互不干扰，无需移除白山
"""

import time
from loguru import logger

from .client import ToukenClient
from .battle import remove_sword, set_sword, run_battle_1_1, get_party_slots, battle_sleep

HAKUSAN_SWORD_IDS:   set[int] = {164, 165}  # 白山吉光（164=未極化, 165=極化）
HAKUSAN_PARTY_NO:    int = 1     # 白山吉光专属治疗队（槽1为固定队长，平时只放白山）
HAKUSAN_MOBILE:      int = 54
HAKUSAN_FATIGUE_MIN:      int = 25  # 治疗前最低气力（低于此值先补满再治疗）
HAKUSAN_FATIGUE_HEAL_MIN: int = 50  # 每次治疗后白山气力下限（低于此值立即补满）
HAKUSAN_FATIGUE_MAX: int = 100
MAX_HEAL_ROUNDS:     int = 10

# 气力手动追踪（getpartyinfo 返回值不可靠）
HAKUSAN_HEAL_COST:     int = 16  # 每次治疗1-1净消耗（含神技-20，队长S+4等）
HAKUSAN_RECOVERY_GAIN: int = 20  # 每次恢复1-1净增加（实测值，无神技触发）
_hakusan_fatigue: int = HAKUSAN_FATIGUE_MAX  # 模块级追踪器，初始假设满气力

HEAVY_INJURY_RATIO: float = 0.30  # 游戏实测：HP ≤ 30% 为重伤（官方未公开精确值，玩家实测约 30~31%）

ALL_PARTY_NOS = range(1, 6)

# 大太刀/薙刀 的 sword_id 集合（AOE 会清场，卸装后基础速度仍需 < 54）
ODACHI_NAGINATA_SWORD_IDS: set[int] = {
    # 薙刀
    9,    # 岩融
    140,
    152,
    # 大太刀
    7,
    60,
    133,
    134,
    163,
}


# ── 判断工具 ────────────────────────────────────────────────

def is_heavily_injured(sword: dict) -> bool:
    hp_max = sword.get("hp_max", 1)
    hp = sword.get("hp", hp_max)
    return hp_max > 0 and (hp * 100 // hp_max) <= int(HEAVY_INJURY_RATIO * 100)


def find_hakusan_serial_id(conquest_data: dict) -> int | None:
    """
    从 conquest 数据中找出白山吉光 serial_id。
    优先返回 HAKUSAN_PARTY_NO（部隊1）槽1 的那把；
    若槽1不是白山，则取第一个空闲的白山；都在出征则取第一个。
    """
    all_swords = {int(k): v for k, v in conquest_data.get("sword", {}).items()}

    # 优先：部隊2 槽1
    party2 = conquest_data.get("party", {}).get(str(HAKUSAN_PARTY_NO), {})
    slot1 = (party2.get("slot") or {}).get("1") or (party2.get("slot") or {}).get(1)
    if isinstance(slot1, dict):
        slot1_sid = slot1.get("serial_id")
        if slot1_sid and all_swords.get(slot1_sid, {}).get("sword_id") in HAKUSAN_SWORD_IDS:
            return slot1_sid

    # 回退：任意未出征的白山
    on_expedition: set[int] = set()
    for party in conquest_data.get("party", {}).values():
        if party.get("status") == 2:
            for slot in party.get("slot", {}).values():
                sid = slot.get("serial_id") if isinstance(slot, dict) else slot
                if sid:
                    on_expedition.add(int(sid))

    candidates = [
        sid for sid, sword in all_swords.items()
        if sword.get("sword_id") in HAKUSAN_SWORD_IDS
    ]
    if not candidates:
        return None
    for sid in candidates:
        if sid not in on_expedition:
            return sid
    return candidates[0]


def can_heal_with_hakusan(sword: dict) -> bool:
    """
    所有刀治疗前均卸装，故速度限制仅针对大太刀/薙刀：
      - 大太刀/薙刀 AOE 会清场，卸装后基础速度（mobile-mobile_up）必须 < 54
      - 其他刀种：无限制，直接返回 True
    """
    if sword.get("sword_id") not in ODACHI_NAGINATA_SWORD_IDS:
        return True
    base_mobile = sword.get("mobile", 0) - sword.get("mobile_up", 0)
    return base_mobile < HAKUSAN_MOBILE


# ── 装备快照与还原 ───────────────────────────────────────────

def _snapshot_equip(sword: dict) -> dict:
    return {
        "equip": {
            1: sword.get("equip_serial_id1"),
            2: sword.get("equip_serial_id2"),
            3: sword.get("equip_serial_id3"),
            4: sword.get("equip_serial_id4"),
        },
        "horse": sword.get("horse_serial_id"),
        "item": sword.get("item_id"),
        "artifact": {
            1: sword.get("artifact_serial_id1"),
            2: sword.get("artifact_serial_id2"),
        },
    }


def _has_any_equip(snapshot: dict) -> bool:
    return any([
        any(v for v in snapshot["equip"].values()),
        snapshot["horse"],
        snapshot["item"],
        any(v for v in snapshot["artifact"].values()),
    ])


def _remove_all_equip(client: ToukenClient, serial_id: int) -> None:
    logger.info(f"  卸除 serial_id={serial_id} 全部刀装（equip/removeall）")
    client._post("equip/removeall", extra={"sword_serial_id": serial_id})
    battle_sleep()


def _restore_equip(client: ToukenClient, serial_id: int, snapshot: dict, skip_item: bool = False) -> None:
    """
    还原刀装快照。skip_item=True 时不还原御守（治疗流程专用，御守在治疗中已消耗或需卸除）。
    还原前先 removeall 确保干净状态（移除可能残留的御守等）。
    """
    logger.info(f"  还原 serial_id={serial_id} 刀装...")
    # 先清空（移除治疗时装的御守等可能残留的装备）
    client._post("equip/removeall", extra={"sword_serial_id": serial_id})
    battle_sleep()
    for slot_no, equip_sid in snapshot["equip"].items():
        if equip_sid:
            client._post("equip/setequip", extra={
                "sword_serial_id": serial_id,
                "serial_id":       equip_sid,
                "slot_no":         slot_no,
            })
            battle_sleep()
    if snapshot["horse"]:
        client._post("equip/setequip", extra={
            "sword_serial_id": serial_id,
            "serial_id":       snapshot["horse"],
            "slot_no":         0,
        })
        battle_sleep()
    if not skip_item and snapshot["item"]:
        client._post("equip/setitem", extra={
            "sword_serial_id": serial_id,
            "consumable_id":   snapshot["item"],
        })
        battle_sleep()
    for slot_no, artifact_sid in snapshot["artifact"].items():
        if artifact_sid:
            client._post("equip/setartifact", extra={
                "sword_serial_id":    serial_id,
                "artifact_serial_id": artifact_sid,
                "slot_no":            slot_no,
            })
            battle_sleep()
    logger.info(f"  serial_id={serial_id} 刀装已还原{' (御守已卸除)' if skip_item else ''}")


# ── 白山吉光气力保障 ─────────────────────────────────────────

def ensure_hakusan_fatigue(client: ToukenClient, min_fatigue: int = HAKUSAN_FATIGUE_MIN) -> None:
    """
    检查白山吉光气力，若 < min_fatigue，刷 1-1 恢复。
    getpartyinfo 返回的气力值不可靠（始终100），改用手动追踪。
    每次治疗1-1消耗 HAKUSAN_HEAL_COST，每次恢复1-1增加 HAKUSAN_RECOVERY_GAIN。
    """
    global _hakusan_fatigue

    if _hakusan_fatigue >= min_fatigue:
        logger.debug(f"白山吉光气力（追踪值）={_hakusan_fatigue}，无需恢复")
        return

    logger.info(f"白山吉光气力（追踪值）={_hakusan_fatigue} < {min_fatigue}，开始恢复...")

    max_rounds = 25
    for round_no in range(1, max_rounds + 1):
        if _hakusan_fatigue >= HAKUSAN_FATIGUE_MAX:
            logger.info(f"  白山吉光气力已恢复至{_hakusan_fatigue}（{round_no - 1}轮）")
            break
        logger.info(f"  round {round_no}：气力={_hakusan_fatigue}，跑1-1恢复中...")
        run_battle_1_1(client, HAKUSAN_PARTY_NO)
        _hakusan_fatigue = min(_hakusan_fatigue + HAKUSAN_RECOVERY_GAIN, HAKUSAN_FATIGUE_MAX)
    else:
        logger.warning(f"白山吉光气力恢复超出{max_rounds}轮，当前追踪值={_hakusan_fatigue}")


def record_heal_cost() -> None:
    """治疗1-1后调用，扣除白山气力消耗"""
    global _hakusan_fatigue
    _hakusan_fatigue = max(_hakusan_fatigue - HAKUSAN_HEAL_COST, 0)
    logger.debug(f"白山吉光气力消耗 -{HAKUSAN_HEAL_COST}，追踪值={_hakusan_fatigue}")


# ── 核心入口 ─────────────────────────────────────────────────

def run_all_repairs(
    client: ToukenClient,
    partylist_sword_data: dict[int, dict] | None = None,
) -> None:
    """
    检查全部 5 支队伍，批量治疗所有重伤刀。
    部隊1 为固定治疗队（白山吉光常驻槽1，平时只放白山）。
    治疗前快照 部隊1 槽2-6，治疗完成后还原。

    partylist_sword_data: 由 party/list 预取的 {serial_id: sword_dict}，
        HP 数据比 conquest 准确（用于 eventsally 失败的队长重伤场景）。
        若不传则用 conquest 数据做重伤检测。
    """
    global _hakusan_fatigue
    ensure_hakusan_fatigue(client)

    conquest_data = client.get_conquest_data()
    conquest_swords = {int(k): v for k, v in conquest_data.get("sword", {}).items()}

    hakusan_sid = find_hakusan_serial_id(conquest_data)
    if hakusan_sid is None:
        logger.error("找不到白山吉光，无法执行重伤治疗")
        return
    logger.debug(f"白山吉光 serial_id={hakusan_sid}")

    party_statuses = {
        int(k): v.get("status") for k, v in conquest_data.get("party", {}).items()
    }

    # 1. 构建准确 HP 数据
    #    conquest sword 字段无 HP；getpartyinfo 才有准确 HP。
    #    有注入数据（地下城 HP 准确窗口内传入）直接用；否则逐队调用 getpartyinfo。
    partyinfo_cache: dict[int, dict] = {}
    if partylist_sword_data is not None:
        all_swords = partylist_sword_data
    else:
        all_swords = dict(conquest_swords)
        for pno in ALL_PARTY_NOS:
            if party_statuses.get(pno) == 2:
                continue
            pi_resp = client._post("party/getpartyinfo", extra={"party_no": pno})
            partyinfo_cache[pno] = pi_resp
            # API 返回格式：sword[serial_id_str] = {...}，值中无 serial_id 字段，需从键取
            for str_sid, sd in pi_resp.get("sword", {}).items():
                try:
                    sid_int = int(str_sid)
                except (ValueError, TypeError):
                    continue
                # conquest 保留静态字段（sword_id/mobile 等），getpartyinfo 覆盖 HP/fatigue
                all_swords[sid_int] = {**conquest_swords.get(sid_int, {}), **sd}

    # 2. 找出全部重伤刀
    to_heal: list[tuple[int, int, int]] = []  # (pno, order, sid)
    for pno in ALL_PARTY_NOS:
        if party_statuses.get(pno) == 2:
            logger.debug(f"部隊{pno} 出征中，跳过重伤检查")
            continue
        slots = get_party_slots(conquest_data, pno)
        for order, sid in slots.items():
            if sid == hakusan_sid:
                continue
            sword = all_swords.get(sid)
            if sword is None or not is_heavily_injured(sword):
                continue
            hp, hp_max = sword.get("hp"), sword.get("hp_max")
            sword_meta = sword if sword.get("sword_id") else conquest_swords.get(sid, sword)
            if not can_heal_with_hakusan(sword_meta):
                base_mobile = sword_meta.get("mobile", 0) - sword_meta.get("mobile_up", 0)
                logger.warning(
                    f"部隊{pno} serial_id={sid} 大太刀/薙刀重伤，"
                    f"卸装后基础速度({base_mobile})≥白山({HAKUSAN_MOBILE})，无法自动治疗"
                )
                continue
            logger.info(
                f"部隊{pno} serial_id={sid} 重伤 HP={hp}/{hp_max} "
                f"({hp * 100 // hp_max}%)，加入治疗队列"
            )
            to_heal.append((pno, order, sid))

    if not to_heal:
        logger.info("全队无重伤刀")
        return

    logger.info(f"共 {len(to_heal)} 把重伤刀，开始治疗（部隊{HAKUSAN_PARTY_NO}）...")

    # 白山当前御守：优先复用 partyinfo_cache，无缓存时单独查
    if HAKUSAN_PARTY_NO in partyinfo_cache:
        party2_swords = partyinfo_cache[HAKUSAN_PARTY_NO].get("sword", {})
    else:
        party2_resp = client._post("party/getpartyinfo", extra={"party_no": HAKUSAN_PARTY_NO})
        party2_swords = party2_resp.get("sword", {})
    hakusan_item_id = None
    for sid_str, sword_data in party2_swords.items():
        try:
            if int(sid_str) == hakusan_sid:
                hakusan_item_id = sword_data.get("item_id")
                break
        except (ValueError, TypeError):
            pass
    # 御守借用暂停（equip/setitem 返回 status=2，原因待查）
    hakusan_item_id = None
    # if hakusan_item_id:
    #     logger.info(f"  白山御守 consumable_id={hakusan_item_id}，治疗时循环借用")
    # else:
    #     logger.debug("  白山未装备御守，跳过御守借用流程")

    # 2. 快照 部隊2 槽2-6（治疗完成后还原）
    heal_slots = get_party_slots(conquest_data, HAKUSAN_PARTY_NO)
    party2_snapshot: dict[int, int] = {
        order: sid for order, sid in heal_slots.items() if order != 1
    }

    # 3. 清空 部隊2 槽2-6（白山保留在槽1）
    for order, sid in sorted(heal_slots.items()):
        if order == 1:
            continue
        remove_sword(client, HAKUSAN_PARTY_NO, order, sid)
        battle_sleep()

    # 4. 逐一治疗
    for orig_pno, orig_order, injured_sid in to_heal:
        logger.info(f"治疗 serial_id={injured_sid}（原队伍 部隊{orig_pno} 槽{orig_order}）...")

        # 每把刀治疗前确认白山气力充足，防止重伤刀在1-1中被打碎
        ensure_hakusan_fatigue(client)

        # 刷新槽位数据（conquest 含槽位，无 HP/equip；equip 需用 getpartyinfo）
        fresh_conquest = client.get_conquest_data()
        # 装备快照：从已合并 getpartyinfo 的 all_swords 取（含 equip_serial_id 等字段）
        sword_data = all_swords.get(injured_sid, {})
        equip_snapshot = _snapshot_equip(sword_data)

        # 若重伤刀为原队队长（槽1），需先在原队内换出队长位才能跨队移动
        # set_sword(orig, 1, other) → other 升为队长，injured 退到 other 的原槽位
        displaced_order: int | None = None
        displaced_sid:   int | None = None
        if orig_order == 1 and orig_pno != HAKUSAN_PARTY_NO:
            orig_slots = get_party_slots(fresh_conquest, orig_pno)
            displaced_order = next((o for o in sorted(orig_slots) if o != 1), None)
            if displaced_order is not None:
                displaced_sid = orig_slots[displaced_order]
                logger.debug(
                    f"  重伤刀为队长，先将 serial_id={displaced_sid}"
                    f"（槽{displaced_order}）升为临时队长..."
                )
                set_sword(client, orig_pno, 1, displaced_sid)
                battle_sleep()
                # 此后：displaced_sid 在槽1，injured_sid 在 displaced_order 槽

        # 重伤刀放入槽2
        set_sword(client, HAKUSAN_PARTY_NO, 2, injured_sid)
        battle_sleep()

        # 卸装
        has_equip = _has_any_equip(equip_snapshot)
        if has_equip:
            _remove_all_equip(client, injured_sid)
        else:
            logger.debug(f"  serial_id={injured_sid} 无装备，跳过 removeall")

        # 从白山借用御守（setitem 会自动从白山转移到重伤刀）
        if hakusan_item_id:
            logger.info(f"  从白山借用御守（consumable_id={hakusan_item_id}）")
            client._post("equip/setitem", extra={
                "sword_serial_id": injured_sid,
                "consumable_id":   hakusan_item_id,
            })
            battle_sleep()

        # 出阵 1-1，仅一轮；治疗后仍重伤视为 bug，终止程序
        run_battle_1_1(client, HAKUSAN_PARTY_NO)
        record_heal_cost()
        # conquest 无 HP 字段，必须用 getpartyinfo 验证治疗结果
        heal_pi = client._post("party/getpartyinfo", extra={"party_no": HAKUSAN_PARTY_NO})
        battle_sleep()
        fresh_sword = heal_pi.get("sword", {}).get(str(injured_sid), {})
        hp_now     = fresh_sword.get("hp")
        hp_max_now = fresh_sword.get("hp_max")
        if is_heavily_injured(fresh_sword):
            logger.error(
                f"  serial_id={injured_sid} 一轮1-1后仍重伤"
                f"（HP={hp_now}/{hp_max_now}），治疗失败，终止程序"
            )
            raise RuntimeError(
                f"治疗失败：serial_id={injured_sid} 1-1后仍重伤（HP={hp_now}/{hp_max_now}）"
            )
        logger.info(f"  serial_id={injured_sid} 治疗成功（HP={hp_now}/{hp_max_now}）")

        # 从 heal_pi 校准白山气力追踪值（1-1 结束后 getpartyinfo 数据新鲜）
        hakusan_sword_data = heal_pi.get("sword", {}).get(str(hakusan_sid), {})
        real_fatigue = hakusan_sword_data.get("fatigue")
        if real_fatigue is not None:
            if real_fatigue != _hakusan_fatigue:
                logger.info(f"  白山气力校准：追踪值={_hakusan_fatigue} → 实际值={real_fatigue}")
                _hakusan_fatigue = real_fatigue
            else:
                logger.debug(f"  白山气力校准：追踪值={_hakusan_fatigue} = 实际值，无需调整")
        else:
            logger.debug("  getpartyinfo 无 fatigue 字段，跳过校准")

        # 治疗后检查白山气力（追踪值），低于 HAKUSAN_FATIGUE_HEAL_MIN 则立即补满再治下一把
        if _hakusan_fatigue < HAKUSAN_FATIGUE_HEAL_MIN:
            logger.info(f"  白山吉光治疗后气力（追踪值）={_hakusan_fatigue} < {HAKUSAN_FATIGUE_HEAL_MIN}，先补满再继续...")
            ensure_hakusan_fatigue(client, min_fatigue=HAKUSAN_FATIGUE_HEAL_MIN)

        # 御守归还白山（setitem 会自动从重伤刀转回白山）
        if hakusan_item_id:
            logger.info(f"  御守归还白山（consumable_id={hakusan_item_id}）")
            client._post("equip/setitem", extra={
                "sword_serial_id": hakusan_sid,
                "consumable_id":   hakusan_item_id,
            })
            battle_sleep()

        # 移出槽2，还原刀装（重伤刀自身御守不还原，skip_item=True）
        remove_sword(client, HAKUSAN_PARTY_NO, 2, injured_sid)
        battle_sleep()

        if has_equip:
            _restore_equip(client, injured_sid, equip_snapshot, skip_item=True)

        # 原来没有刀装（只有马/宝物/什么都没有）→ 自动装备刀装
        has_touso = any(v for v in equip_snapshot["equip"].values())
        if not has_touso:
            logger.info(f"  serial_id={injured_sid} 原无刀装，治疗后自动装备...")
            client._post("equip/autosetequip", extra={
                "sword_serial_id": injured_sid,
                "is_event":        0,
                "is_firstattack":  1,
            })
            battle_sleep()
            # 验证是否真的装上了刀装
            verify_resp = client._post("party/getpartyinfo", extra={"party_no": HAKUSAN_PARTY_NO})
            battle_sleep()
            sword_after = verify_resp.get("sword", {}).get(str(injured_sid), {})
            equipped = any(
                sword_after.get(f"equip_serial_id{i}") for i in range(1, 5)
            )
            if not equipped:
                logger.error(
                    f"  serial_id={injured_sid} 自动装备失败（刀装库存不足？），"
                    f"裸装出阵风险极高，停止运行"
                )
                raise RuntimeError(f"autosetequip 失败：serial_id={injured_sid} 无可用刀装")

        # 归还至原队伍（部隊2的伤刀由 party2_snapshot 还原，不重复操作）
        if orig_pno != HAKUSAN_PARTY_NO:
            if orig_order == 1 and displaced_order is not None and displaced_sid is not None:
                # 两步还原：先把重伤刀放回原队（非队长槽），再换回队长位
                set_sword(client, orig_pno, displaced_order, injured_sid)
                battle_sleep()
                set_sword(client, orig_pno, 1, injured_sid)
            else:
                set_sword(client, orig_pno, orig_order, injured_sid)
            battle_sleep()
            logger.info(f"  serial_id={injured_sid} 已归还至 部隊{orig_pno} 槽{orig_order}")

    # 5. 还原 部隊2 槽2-6
    if party2_snapshot:
        logger.info(f"还原 部隊{HAKUSAN_PARTY_NO} 原编成...")
        for order, sid in sorted(party2_snapshot.items()):
            set_sword(client, HAKUSAN_PARTY_NO, order, sid)
            battle_sleep()

    logger.info("所有重伤治疗完成")


# ── 单队检查入口（供出征模块按需调用）────────────────────────

def run_repair_check(client: ToukenClient, party_no: int) -> bool:
    """
    检查单支队伍是否有重伤刀。
    使用 party/getpartyinfo 读取准确 HP 数据（conquest HP 可能不准）。
    有则触发 run_all_repairs（全队扫描+治疗），返回 True；否则返回 False。
    """
    partyinfo_resp = client._post("party/getpartyinfo", extra={"party_no": party_no})
    has_injury = any(
        is_heavily_injured(sword_data)
        for sword_data in partyinfo_resp.get("sword", {}).values()
    )
    if has_injury:
        run_all_repairs(client)
        return True
    return False

"""
气力恢复模块 —— 指定队伍全员跑1-1补满气力

流程：
  1. 输入队伍编号
  2. getpartyinfo 检查全员气力
  3. 气力 < 100 的刀轮流跑1-1补满
  4. 全员满气力 → 结束
"""

from loguru import logger

from .client import ToukenClient
from .battle import (
    battle_sleep, get_party_slots, run_battle_1_1,
    set_sword, remove_sword, FATIGUE_TARGET,
)


def run_fatigue_recovery(client: ToukenClient, party_no: int) -> None:
    """
    指定队伍全员补满气力。
    逐刀放到队长位跑1-1，恢复后归位，直到全员气力 >= FATIGUE_TARGET。
    """
    logger.info(f"气力恢复开始（部隊{party_no}）")

    # 获取队伍信息和气力
    pi = client._post("party/getpartyinfo", extra={"party_no": party_no})
    battle_sleep()
    swords = pi.get("sword", {})

    if not swords:
        logger.warning(f"部隊{party_no} 无刀剑数据")
        return

    # 找出需要恢复的刀
    low_swords = []
    for sid_str, data in swords.items():
        fat = data.get("fatigue", 100)
        sid = int(sid_str)
        if fat < FATIGUE_TARGET:
            low_swords.append((sid, fat))
        else:
            logger.info(f"  serial_id={sid} 气力={fat}，无需恢复")

    if not low_swords:
        logger.info(f"部隊{party_no} 全员气力充足，无需恢复")
        return

    low_swords.sort(key=lambda x: x[1])  # 最低气力优先
    logger.info(f"部隊{party_no} 有 {len(low_swords)} 把刀需要恢复气力")

    # 记录原始槽位
    conquest_data = client.get_conquest_data()
    original_slots = get_party_slots(conquest_data, party_no)

    for target_sid, current_fat in low_swords:
        target_order = next((o for o, sid in original_slots.items() if sid == target_sid), None)
        if target_order is None:
            continue

        logger.info(f"  恢复 serial_id={target_sid}（槽{target_order}，气力={current_fat}）")

        # 把目标刀换到队长位
        if target_order != 1:
            set_sword(client, party_no, 1, target_sid)
            battle_sleep()

        # 移除槽2-6
        cur_data = client.get_conquest_data()
        cur_slots = get_party_slots(cur_data, party_no)
        for order, sid in sorted(cur_slots.items()):
            if order == 1:
                continue
            remove_sword(client, party_no, order, sid)
            battle_sleep()

        # 跑1-1直到满气力
        max_rounds = 10
        for round_no in range(1, max_rounds + 1):
            run_battle_1_1(client, party_no)

            # 检查气力
            check_pi = client._post("party/getpartyinfo", extra={"party_no": party_no})
            battle_sleep()
            sword_data = check_pi.get("sword", {}).get(str(target_sid), {})
            fat = sword_data.get("fatigue", 0)
            logger.info(f"    round {round_no}：气力={fat}")

            if fat >= FATIGUE_TARGET:
                logger.info(f"  serial_id={target_sid} 气力恢复到 {fat}")
                break
        else:
            logger.warning(f"  serial_id={target_sid} 超出 {max_rounds} 轮，气力可能未满")

        # 归位：恢复原队伍
        if target_order != 1:
            set_sword(client, party_no, 1, original_slots[1])
            battle_sleep()
            set_sword(client, party_no, target_order, target_sid)
            battle_sleep()

        # 归位槽2-6
        for order, sid in sorted(original_slots.items()):
            if order == 1:
                continue
            if sid == target_sid:
                continue  # 已归位
            cur_data2 = client.get_conquest_data()
            cur_slots2 = get_party_slots(cur_data2, party_no)
            if order in cur_slots2 and cur_slots2[order] == sid:
                continue  # 已在位
            set_sword(client, party_no, order, sid)
            battle_sleep()

    logger.info(f"部隊{party_no} 全员气力恢复完成")

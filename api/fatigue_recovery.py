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
    api_sleep, get_party_slots, run_battle_1_1,
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
    api_sleep()
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

    # 第一把刀换到队长位
    first_sid = low_swords[0][0]
    first_order = next((o for o, sid in original_slots.items() if sid == first_sid), None)
    if first_order and first_order != 1:
        set_sword(client, party_no, 1, first_sid)
        api_sleep()

    # 一次性清空槽2-6
    cur_data = client.get_conquest_data()
    cur_slots = get_party_slots(cur_data, party_no)
    for order, sid in sorted(cur_slots.items()):
        if order == 1:
            continue
        remove_sword(client, party_no, order, sid)
        api_sleep()

    logger.info("队伍已清场，开始逐刀恢复...")

    # 逐把换队长跑1-1
    for i, (target_sid, current_fat) in enumerate(low_swords):
        logger.info(f"  [{i+1}/{len(low_swords)}] serial_id={target_sid}（气力={current_fat}）")

        # 换队长（第一把已经在位）
        if i > 0:
            set_sword(client, party_no, 1, target_sid)
            api_sleep()

        # 跑1-1直到满气力
        max_rounds = 10
        for round_no in range(1, max_rounds + 1):
            run_battle_1_1(client, party_no)

            check_pi = client._post("party/getpartyinfo", extra={"party_no": party_no})
            api_sleep()
            sword_data = check_pi.get("sword", {}).get(str(target_sid), {})
            fat = sword_data.get("fatigue", 0)
            logger.info(f"    round {round_no}：气力={fat}")

            if fat >= FATIGUE_TARGET:
                logger.info(f"  serial_id={target_sid} 气力恢复完成")
                break
        else:
            logger.warning(f"  serial_id={target_sid} 超出 {max_rounds} 轮")

    # 全部恢复完，一次性还原队伍
    logger.info("全部恢复完成，还原队伍...")
    set_sword(client, party_no, 1, original_slots[1])
    api_sleep()
    for order, sid in sorted(original_slots.items()):
        if order == 1:
            continue
        set_sword(client, party_no, order, sid)
        api_sleep()

    logger.info(f"部隊{party_no} 全员气力恢复完成")

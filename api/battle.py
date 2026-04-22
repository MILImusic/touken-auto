"""
气力恢复模块 —— 通过1-1出阵恢复刀剑气力

流程：
  1. 将气力不足的刀剑单独留在队伍中（其余刀剑临时移除）
  2. 出阵 1-1（sally/sally → sally/startup → forward+battle ×2）
  3. 1-1 结束后自动回本丸，检查气力是否恢复到目标值
  4. 重复直到气力 ≥ FATIGUE_TARGET
  5. 将原队伍刀剑归位（party/setsword）

API 端点（已通过抓包确认）：
  party/removesword  —— 从队伍移除一把刀
  party/setsword     —— 将刀设置到队伍指定槽位
  sally/sally        —— 开始出阵（选图）
  sally/startup      —— 出阵启动（进入地图）
  sally/forward      —— 前进（direction=0）
  battle/battle      —— 战斗（formation_id=4 鱼鳞阵）
  home/index         —— 回本丸后刷新状态（脚本手动调用）
"""

import time
import random
from loguru import logger

from .client import ToukenClient

FATIGUE_TARGET    = 100  # 气力恢复目标
FATIGUE_THRESHOLD = 90   # 低于此值需要恢复


def get_party_fatigue(conquest_data: dict, party_info: dict) -> dict[int, int]:
    """从 conquest 数据中提取指定队伍每把刀的气力值，返回 {serial_id: fatigue}"""
    swords = conquest_data.get("sword", {})
    result = {}
    for slot in party_info.get("slot", {}).values():
        sid = slot.get("serial_id")
        if sid:
            result[sid] = swords.get(str(sid), {}).get("fatigue", FATIGUE_TARGET)
    return result


# 1-1 地图参数
RECOVERY_EPISODE_ID = 1
RECOVERY_FIELD_ID   = 1
RECOVERY_BATTLES    = 2   # 1-1 共两场战斗
FORMATION_ID        = 3   # 逆行阵（侦察失败时的默认阵型）

# 每次出阵之间等待时间（秒），加随机抖动模拟人类操作
BATTLE_DELAY = 0.5  # 基准延迟，实际范围 0.0～1.5s


def battle_sleep() -> None:
    time.sleep(BATTLE_DELAY + random.uniform(-0.5, 1.0))


def remove_sword(client: ToukenClient, party_no: int, order: int, serial_id: int) -> None:
    """从队伍的指定槽位移除一把刀"""
    logger.debug(f"  移除：部隊{party_no} 槽位{order} serial_id={serial_id}")
    client._post("party/removesword", extra={
        "party_no": party_no,
        "order": order,
        "serial_id": serial_id,
    })


def set_sword(client: ToukenClient, party_no: int, order: int, serial_id: int) -> dict:
    """将刀放回队伍的指定槽位，返回 setsword 响应（含完整 party 数据）"""
    logger.debug(f"  归位：部隊{party_no} 槽位{order} serial_id={serial_id}")
    return client._post("party/setsword", extra={
        "party_no": party_no,
        "order": order,
        "serial_id": serial_id,
    })


def get_party_slots(conquest_data: dict, party_no: int) -> dict[int, int]:
    """
    获取队伍的槽位 → serial_id 映射。
    返回 {order: serial_id}，order 为整数（1-6）。
    """
    parties = conquest_data.get("party", {})
    party_info = parties.get(str(party_no), {})
    slots = {}
    for order_str, slot in party_info.get("slot", {}).items():
        sid = slot.get("serial_id")
        if sid:
            slots[int(order_str)] = sid
    return slots


def run_battle_1_1(client: ToukenClient, party_no: int) -> None:
    """
    执行一次 1-1 出阵（共两场战斗，完成后自动回本丸）。
    """
    logger.info(f"  部隊{party_no} 开始出阵 1-1...")

    # 1. 选图
    client._post("sally/sally", extra={
        "episode_id": RECOVERY_EPISODE_ID,
        "field_id":   RECOVERY_FIELD_ID,
        "party_no":   party_no,
    })
    battle_sleep()

    # 2. 启动出阵
    client._post("sally/startup")
    battle_sleep()

    # 3. 两场战斗
    for i in range(1, RECOVERY_BATTLES + 1):
        logger.debug(f"    第{i}场战斗：前进中...")
        client._post("sally/forward", extra={"direction": 0})
        battle_sleep()

        logger.debug(f"    第{i}场战斗：战斗中...")
        client._post("battle/battle", extra={"formation_id": FORMATION_ID})
        battle_sleep()

    # 4. 刷新游戏状态（1-1 结束后服务器已回本丸，客户端同步）
    client.get_game_state()
    logger.info(f"  部隊{party_no} 1-1 结束，已回本丸")


def recover_sword_fatigue(client: ToukenClient, party_no: int, low_fatigue_sids: list[int]) -> None:
    """
    对队伍中气力不足的刀剑进行恢复：
      1. 临时移除队伍中气力 OK 的其他刀剑（仅保留需要恢复的刀）
      2. 多次刷 1-1，直到所有刀剑气力 ≥ FATIGUE_TARGET
      3. 将原队伍刀剑归位

    注意：每次只处理一把刀（单刀模式），依次恢复。
    """
    # 获取当前队伍槽位信息
    conquest_data = client.get_conquest_data()
    slots = get_party_slots(conquest_data, party_no)

    if not slots:
        logger.warning(f"部隊{party_no} 槽位数据为空，跳过气力恢复")
        return

    # 第一把刀换到队长位
    first_sid = low_fatigue_sids[0]
    first_order = next((o for o, sid in slots.items() if sid == first_sid), None)
    if first_order and first_order != 1:
        set_sword(client, party_no, 1, first_sid)
        battle_sleep()

    # 一次性清空槽2-6
    cur_data = client.get_conquest_data()
    cur_slots = get_party_slots(cur_data, party_no)
    for order, sid in sorted(cur_slots.items()):
        if order == 1:
            continue
        remove_sword(client, party_no, order, sid)
        battle_sleep()

    # 逐把换队长跑1-1
    for i, target_sid in enumerate(low_fatigue_sids):
        if i > 0:
            set_sword(client, party_no, 1, target_sid)
            battle_sleep()

        max_rounds = 5
        current_fat = 0
        for round_no in range(1, max_rounds + 1):
            conquest_data = client.get_conquest_data()
            party_info = conquest_data.get("party", {}).get(str(party_no), {})
            fatigue_map = get_party_fatigue(conquest_data, party_info)
            current_fat = fatigue_map.get(target_sid, 0)

            logger.info(f"  round {round_no}：serial_id={target_sid} 气力={current_fat}")
            if current_fat >= FATIGUE_TARGET:
                logger.info(f"  serial_id={target_sid} 气力恢复完成")
                break

            run_battle_1_1(client, party_no)
        else:
            logger.warning(f"  serial_id={target_sid} 气力恢复超出 {max_rounds} 轮，当前值：{current_fat}")
            if current_fat == 0:
                _notify_fatigue_stuck(target_sid, max_rounds)

    # 全部完成，一次性还原队伍
    logger.info(f"  部隊{party_no} 还原队伍...")
    set_sword(client, party_no, 1, slots[1])
    battle_sleep()
    for order, sid in sorted(slots.items()):
        if order == 1:
            continue
        set_sword(client, party_no, order, sid)
        battle_sleep()
    client.get_conquest_data()
    logger.info(f"部隊{party_no} 气力恢复完成")


def _notify_fatigue_stuck(serial_id: int, rounds: int) -> None:
    """气力恢复卡在0时发 Telegram 通知"""
    import os
    import httpx
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = "YOUR_TELEGRAM_CHAT_ID"
    if not bot_token:
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": f"⚠ 气力恢复异常\nserial_id={serial_id} 跑了 {rounds} 轮 1-1 后气力仍为 0（可能过疲劳）"},
            timeout=10,
        )
    except Exception:
        pass

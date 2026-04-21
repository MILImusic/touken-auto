"""
演练模块

流程（每个对手一次循环）：
  1. practice/offer        — 选择对手（num=1~5）→ JSON（对手刀剑数据）
  2. battle/practicescout  — 选择出战队伍 + 对手编号 → JSON（含 formation_id）
     * status=2            → 该对手今日已打完，跳过
     * formation_id != 0   → 游戏自动推荐阵型
     * formation_id == 0   → 侦查不足，需手动选阵型
  3. battle/practicebattle — 出战，固定使用 PRACTICE_FORMATION_ID → HTML
  演练结束后游戏自动返回演练列表（不需要额外请求）

配置：
  PRACTICE_PARTY_NO     = 3   # 演练出战队伍
  PRACTICE_FORMATION_ID = 3   # 逆行阵（侦查不足时的默认阵型）
  PRACTICE_OPPONENTS    = 5   # 对手总数（num=1~5 全打）
"""

import time
from loguru import logger

from .client import ToukenClient
from .battle import battle_sleep

PRACTICE_PARTY_NO:     int = 3
PRACTICE_FORMATION_ID: int = 3   # 逆行阵
PRACTICE_OPPONENTS:    int = 5   # 对手编号 1~5


def run_practice_cycle(client: ToukenClient) -> None:
    """
    依次对 1~PRACTICE_OPPONENTS 号对手完成演练。
    每场：offer → practicescout → practicebattle
    """
    # 打开演练页（HTML UI 入口，token via Set-Cookie）
    client._post("practice")
    battle_sleep()

    completed = 0
    for num in range(1, PRACTICE_OPPONENTS + 1):
        logger.info(f"演练：对手 {num}/{PRACTICE_OPPONENTS}")

        # 1. 选择对手
        client._post("practice/offer", extra={"num": num})
        battle_sleep()

        # 2. 扫描（选出战队伍）；status=2 表示该对手本日已打完
        scout_resp = client._post("battle/practicescout", extra={
            "party_no": PRACTICE_PARTY_NO,
            "num": num,
        })
        battle_sleep()

        if scout_resp.get("status") == 2:
            logger.info(f"  对手{num}：今日已完成，跳过")
            completed += 1
            continue

        formation_id = scout_resp.get("formation_id", 0)
        if formation_id == 0:
            logger.info(
                f"  对手{num}：侦查不足（formation_id=0），"
                f"使用备用阵型 {PRACTICE_FORMATION_ID}"
            )
        else:
            logger.info(f"  对手{num}：侦查充足（formation_id={formation_id}），出战")

        # 3. 出战（固定使用鶴翼の陣）
        client._post("battle/practicebattle", extra={
            "formation_id": PRACTICE_FORMATION_ID,
        })
        logger.info(f"  对手{num} 演练完成")
        battle_sleep()
        completed += 1

    if completed == PRACTICE_OPPONENTS:
        logger.info(f"全部 {PRACTICE_OPPONENTS} 场演练完成")
    else:
        logger.info(f"演练完成 {completed}/{PRACTICE_OPPONENTS} 场")

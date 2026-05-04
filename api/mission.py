"""
任务奖励模块

流程：
  1. 从 home/index 的 badge_count.mission_achieved 判断是否有可领取任务
  2. 依次尝试三个 category：
       category=1  主线任务  event_id=0
       category=2  每日任务  event_id=0
       category=5  活动任务  event_id=state["ev"]（当前活动ID）
  3. mission/rewards  — count 传入 badge_count 作为上限，服务端按实际可领取数处理

注意：
  - mission/index 无 JSON 响应，无法从中获取精确 count
  - 每次领取后检查 finish_mission_ids 确认实际领取数量
"""

from loguru import logger

from .client import ToukenClient
from .battle import api_sleep

MISSION_CATEGORY_MAIN:  int = 1   # 主线任务
MISSION_CATEGORY_DAILY: int = 2   # 每日任务
MISSION_CATEGORY_EVENT: int = 5   # 活动任务


def _claim(client: ToukenClient, category: int, count: int, event_id: int, label: str) -> int:
    """
    尝试领取指定 category 的任务奖励。
    返回实际领取数量（finish_mission_ids 长度）。
    """
    resp = client._post("mission/rewards", extra={
        "category": category,
        "count":    count,
        "event_id": event_id,
    })
    api_sleep()

    if resp.get("status", 0) != 0:
        logger.debug(f"  {label}：无可领取（status={resp.get('status')}）")
        return 0

    finished = resp.get("finish_mission_ids", [])
    if not finished:
        logger.info(f"  {label}：无可领取")
        return 0

    items = resp.get("item", [])
    if items:
        summary = ", ".join(
            f"item_id={i['item_id']} ×{i['item_num']}" for i in items
        )
        logger.info(f"  {label}：领取 {len(finished)} 个 → 奖励：{summary}")
    else:
        logger.info(f"  {label}：领取 {len(finished)} 个")

    return len(finished)


def run_mission_rewards(client: ToukenClient, state: dict) -> None:
    """
    检查并领取所有可领取的任务奖励（主线 / 每日 / 活动）。
    state 来自 home/index 响应。
    """
    badge = state.get("badge_count", {}).get("mission_achieved", 0)
    if badge == 0:
        logger.info("任务奖励：无可领取（badge=0），跳过")
        return

    logger.info(f"任务奖励：检测到 {badge} 个可领取，开始领取...")
    event_id = state.get("ev", 0)

    # 先进入任务页（导航用，无 JSON 响应）
    client._post("mission/index")
    api_sleep()

    total = 0
    total += _claim(client, MISSION_CATEGORY_MAIN,  badge, 0,        "主线任务")
    total += _claim(client, MISSION_CATEGORY_DAILY, badge, 0,        "每日任务")
    if event_id:
        total += _claim(client, MISSION_CATEGORY_EVENT, badge, event_id, "活动任务")

    logger.info(f"任务奖励：共领取 {total} 个")

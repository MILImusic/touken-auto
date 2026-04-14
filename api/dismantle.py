"""
刀解模块 —— 自动领取非短刀 + 刀解释放刀位

流程：
  1. receive/list (sword_type=2,6,5,7,3,4,10) → 筛选非短刀
  2. receive/get (serial_ids) → 领取（最多100把/次）
  3. forge → 获取所有刀剑数据
  4. 筛选可刀解的刀（protect==0，不在队伍/内番，且不需要留给习合）
  5. forge/dismantle (serial_id=逗号分隔，最多30) → 刀解
  6. 循环直到受取箱空+无可刀解的刀

安全约束：
  - 绝不碰 protect >= 1 的刀
  - 同 sword_id 存在保护刀且 ranbu_level < 10 → 留给习合，不刀解
  - 不刀解编队中（role_id != 0）或内番中的刀

配置：
  NON_TANTOU_SWORD_TYPES = "2,6,5,7,3,4,10"
  MAX_DISMANTLE_PER_BATCH = 30
  MAX_RECEIVE_PER_BATCH = 100
"""

from loguru import logger

from .client import ToukenClient
from .battle import battle_sleep
from .composition import _load_tantou_db

NON_TANTOU_SWORD_TYPES: str = "2,6,5,7,3,4,10"
MAX_DISMANTLE_PER_BATCH: int = 30
MAX_RECEIVE_PER_BATCH: int = 100


def _receive_non_tantou(client: ToukenClient) -> int:
    """
    从受取箱领取非短刀。
    返回领取数量。
    """
    list_resp = client._post("receive/list", extra={
        "sort_type": 2,
        "order": 2,
        "item_type": 0,
        "sword_type": NON_TANTOU_SWORD_TYPES,
    })
    battle_sleep()

    receive_items = list_resp.get("receive", {})
    if not receive_items:
        logger.info("受取箱无非短刀可领取")
        return 0

    all_sids = list(receive_items.keys())
    batch_sids = all_sids[:MAX_RECEIVE_PER_BATCH]

    logger.info(f"受取箱有 {len(all_sids)} 把非短刀，本次领取 {len(batch_sids)} 把")

    serial_ids_str = ",".join(batch_sids)
    resp = client._post("receive/get", extra={
        "serial_ids": serial_ids_str,
        "sort_type": 2,
        "order": 2,
        "item_type": 0,
        "sword_type": NON_TANTOU_SWORD_TYPES,
    })
    battle_sleep()

    received = resp.get("serial_ids", [])
    received_count = len(received) if received else 0
    if received_count:
        logger.info(f"成功领取 {received_count} 把非短刀")
    else:
        logger.warning("receive/get 返回空，领取可能失败（刀位满？）")

    return received_count


def _get_forge_data(client: ToukenClient) -> dict:
    """调用 forge 获取所有刀剑数据"""
    resp = client._post("forge")
    battle_sleep()
    return resp


def _find_dismantleable(forge_data: dict, tantou_sword_ids: set[int]) -> list[int]:
    """
    从 forge 数据中找出可刀解的 serial_id 列表。

    可刀解 = protect==0 且不在队伍/内番 且：
      - 同 sword_id 没有保护刀（没有本体）
      - 或同 sword_id 的保护刀 ranbu_level == 10（已满级）

    不可刀解 = 同 sword_id 存在保护刀且 ranbu_level < 10（留给习合）
    """
    swords = forge_data.get("sword", {})

    # 提取内番中的 serial_id
    duty = forge_data.get("duty", {})
    duty_sids: set[int] = set()
    if isinstance(duty, list):
        duty_sids = set(duty)
    elif isinstance(duty, dict):
        for key, val in duty.items():
            if isinstance(val, int):
                duty_sids.add(val)

    # 构建保护刀的 sword_id → max_ranbu_level 映射
    protected_ranbu: dict[int, int] = {}
    for sword in swords.values():
        if sword.get("protect", 0) >= 1:
            sid = sword.get("sword_id")
            ranbu = sword.get("ranbu_level", 0)
            protected_ranbu[sid] = max(protected_ranbu.get(sid, 0), ranbu)

    # 筛选可刀解的
    dismantleable: list[int] = []
    for sword in swords.values():
        if sword.get("protect", 0) != 0:
            continue
        if sword.get("role_id", 0) != 0:
            continue
        if sword.get("serial_id") in duty_sids:
            continue

        sword_id = sword.get("sword_id")

        # 短刀留给习合模块处理
        if sword_id in tantou_sword_ids:
            continue

        # 检查是否有保护刀需要这个 sword_id 做习合素材
        if sword_id in protected_ranbu and protected_ranbu[sword_id] < 10:
            continue  # 留给习合

        dismantleable.append(sword["serial_id"])

    return dismantleable


def _do_dismantle(client: ToukenClient, serial_ids: list[int]) -> None:
    """执行一次刀解"""
    serial_id_str = ",".join(str(sid) for sid in serial_ids)
    client._post("forge/dismantle", extra={
        "serial_id": serial_id_str,
    })
    battle_sleep()
    logger.info(f"  刀解完成：{len(serial_ids)} 把")


def run_dismantle_cycle(client: ToukenClient) -> None:
    """
    自动刀解循环：领取非短刀 → 刀解 → 领更多 → 直到无刀可领+无可刀解。
    """
    logger.info("刀解循环开始")
    tantou_sword_ids = _load_tantou_db()
    total_dismantled = 0

    try:
        while True:
            # 1. 领取非短刀
            received_count = _receive_non_tantou(client)

            # 2. 获取 forge 数据
            forge_data = _get_forge_data(client)

            # 3. 筛选可刀解的
            dismantleable = _find_dismantleable(forge_data, tantou_sword_ids)

            if not dismantleable:
                if received_count == 0:
                    logger.info("无非短刀可领取，无可刀解的刀，循环结束")
                    break
                else:
                    logger.info("本轮领取了刀但无可刀解（可能都需要留给习合），继续领取...")
                    continue

            logger.info(f"找到 {len(dismantleable)} 把可刀解的刀")

            # 4. 分批刀解（每次最多30把）
            for i in range(0, len(dismantleable), MAX_DISMANTLE_PER_BATCH):
                batch = dismantleable[i:i + MAX_DISMANTLE_PER_BATCH]
                _do_dismantle(client, batch)
                total_dismantled += len(batch)

    except KeyboardInterrupt:
        logger.info(f"刀解循环中断 — 共刀解 {total_dismantled} 把")
        raise

    logger.info(f"刀解循环完成 — 共刀解 {total_dismantled} 把")

"""
限时锻造活动模块 —— 自动领取 + 刀解/习合清位 + 再开十连

流程（在 sortie 休息间隙执行）：
  1. 从 home/index 的 situation.forge 检查 4 个槽的 finished_at
  2. 到时间的槽：先清仓库（刀解/习合，不领受取箱）腾出 10 位
  3. forge/completemultiple 领取十连
  4. 检查 is_new_sword → 新刀则停止锻造，发 Telegram 通知
  5. 非新刀 → 把刚领的也清掉（刀解/习合）
  6. forge/startmultiple 开新一轮
  7. 资源不够或全槽锻造中 → 跳过

端点（2026-04-21 抓包）：
  forge/startmultiple    — 开始十连锻造
    参数：slot_no, charcoal, steel, coolant, file, consumable_id=0,
          use_assist=0, mileage_event_id
    返回：无 JSON body（token 从 Set-Cookie 取）
  forge/completemultiple — 领取十连锻造
    参数：slot_no
    返回：sword[] (10把, 含 sword_id, is_new_sword, serial_id)

配置：
  FORGE_SLOTS = 4
  FORGE_RECIPE = {charcoal: 700, steel: 700, coolant: 700, file: 700}
  MILEAGE_EVENT_ID = 10498  （当前活动，需手动更新）
"""

import datetime
from loguru import logger

from .client import ToukenClient
from .battle import battle_sleep
from .composition import get_sword_name

# ── 配置 ──────────────────────────────────────────────────────
FORGE_SLOTS: int = 4                # 锻造栏位数
FORGE_RECIPE: dict[str, int] = {
    "charcoal": 700,
    "steel": 700,
    "coolant": 700,
    "file": 700,
}
MILEAGE_EVENT_ID: int = 10498       # 当前活动 ID（每次活动需更新）
MIN_FREE_SLOTS: int = 10            # 领取前至少需要的空位


def check_forge_slots(state: dict) -> list[int]:
    """
    从 home/index 响应检查哪些锻造槽已完成。
    返回已完成的 slot_no 列表。
    """
    forge_status = state.get("situation", {}).get("forge", {})
    now_str = state.get("now", "")
    if not now_str or not forge_status:
        return []

    try:
        now = datetime.datetime.strptime(now_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return []

    completed = []
    for slot_str, slot_data in forge_status.items():
        if not isinstance(slot_data, dict):
            continue
        status = slot_data.get("status", 0)
        finished_at = slot_data.get("finished_at", "")
        if status != 2 or not finished_at:
            continue
        try:
            finish_time = datetime.datetime.strptime(finished_at, "%Y-%m-%d %H:%M:%S")
            if now >= finish_time:
                completed.append(int(slot_str))
        except (ValueError, TypeError):
            continue

    return sorted(completed)


def _clean_inventory(client: ToukenClient) -> int:
    """
    清理仓库（刀解 + 习合保护刀），不领受取箱。
    返回释放的刀位数。
    """
    from .dismantle import _get_forge_data, _classify_swords, _do_dismantle, \
        _do_composition_for_protected, MAX_DISMANTLE_PER_BATCH
    from .composition import _load_tantou_db

    tantou_sword_ids = _load_tantou_db()
    forge_data = _get_forge_data(client)
    dismantleable, comp_targets = _classify_swords(forge_data, tantou_sword_ids)

    freed = 0

    # 先习合保护刀
    if comp_targets:
        fed = _do_composition_for_protected(client, comp_targets)
        if fed > 0:
            forge_data = _get_forge_data(client)
            dismantleable, _ = _classify_swords(forge_data, tantou_sword_ids)

    # 再刀解
    for i in range(0, len(dismantleable), MAX_DISMANTLE_PER_BATCH):
        batch = dismantleable[i:i + MAX_DISMANTLE_PER_BATCH]
        _do_dismantle(client, batch)
        freed += len(batch)

    return freed


def _complete_forge(client: ToukenClient, slot_no: int) -> list[dict]:
    """
    领取一个槽的十连锻造结果。
    返回 sword 列表（每把含 sword_id, is_new_sword, serial_id）。
    """
    resp = client._post("forge/completemultiple", extra={"slot_no": slot_no})
    battle_sleep()
    swords = resp.get("sword", [])
    if swords:
        names = [get_sword_name(s.get("sword_id", 0)) for s in swords]
        logger.info(f"  槽{slot_no} 领取十连：{', '.join(names)}")
    return swords


def _start_forge(client: ToukenClient, slot_no: int) -> bool:
    """
    在指定槽开始十连锻造。
    返回 True 成功，False 失败（资源不足等）。
    """
    extra = {
        "slot_no": slot_no,
        "charcoal": FORGE_RECIPE["charcoal"],
        "steel": FORGE_RECIPE["steel"],
        "coolant": FORGE_RECIPE["coolant"],
        "file": FORGE_RECIPE["file"],
        "consumable_id": 0,
        "use_assist": 0,
        "mileage_event_id": MILEAGE_EVENT_ID,
    }
    resp = client._post("forge/startmultiple", extra=extra)
    battle_sleep()
    status = resp.get("status", 0) if isinstance(resp, dict) else 0
    if status != 0:
        logger.warning(f"  槽{slot_no} 开始锻造失败（status={status}）")
        return False
    logger.info(f"  槽{slot_no} 十连锻造已开始（资源各{FORGE_RECIPE['charcoal']}）")
    return True


def _has_enough_resources(state: dict) -> bool:
    """检查资源是否足够开一轮十连锻造"""
    res = state.get("resource", {})
    for key, needed in FORGE_RECIPE.items():
        if res.get(key, 0) < needed:
            return False
    return True


def run_forge_check(client: ToukenClient, stop_on_new: bool = True) -> bool:
    """
    检查并处理锻造槽。在 sortie 休息间隙调用。

    返回 True 如果发现了新刀（调用者应停止锻造循环）。
    返回 False 正常继续。
    """
    # 1. 获取最新状态
    state = client._post("home/index")
    battle_sleep()

    completed_slots = check_forge_slots(state)
    if not completed_slots:
        return False

    logger.info(f"锻造检查：槽 {completed_slots} 已完成")
    found_new = False

    for slot_no in completed_slots:
        # 2. 先清仓库腾位（不领受取箱）
        freed = _clean_inventory(client)
        if freed > 0:
            logger.info(f"  清理仓库：释放 {freed} 个刀位")

        # 3. 领取十连
        swords = _complete_forge(client, slot_no)

        # 4. 检查新刀
        new_swords = [s for s in swords if s.get("is_new_sword")]
        if new_swords:
            for s in new_swords:
                name = get_sword_name(s.get("sword_id", 0))
                logger.info(f"  ★ 新刀锻出！{name}(id={s.get('sword_id')}) ★")
            found_new = True
            if stop_on_new:
                # 发 Telegram 通知
                _notify_new_sword(new_swords)
                logger.info("  发现新刀，停止后续锻造")
                continue  # 不在这个槽开新锻造，但继续领其他完成的槽

        # 5. 非新刀 → 清掉刚领的
        freed2 = _clean_inventory(client)
        if freed2 > 0:
            logger.info(f"  二次清理：释放 {freed2} 个刀位")

        # 6. 开新一轮（如果没发现新刀且资源够）
        if not found_new:
            state = client._post("home/index")
            battle_sleep()
            if _has_enough_resources(state):
                _start_forge(client, slot_no)
            else:
                logger.info(f"  槽{slot_no} 资源不足，跳过开启新锻造")

    return found_new


def _notify_new_sword(swords: list[dict]) -> None:
    """新刀锻出时发 Telegram 通知"""
    import os
    import httpx

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = "YOUR_TELEGRAM_CHAT_ID"
    if not bot_token:
        return

    names = [f"{get_sword_name(s.get('sword_id', 0))}(id={s.get('sword_id')})" for s in swords]
    text = f"★ 限时锻造新刀！\n{chr(10).join(names)}"
    try:
        httpx.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except Exception:
        pass

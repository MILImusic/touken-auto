"""
远征（出征/回收）操作模块

状态说明：
  party.status == 1  →  在本丸（空闲）
  party.status == 2  →  出征中

situation.conquest[party_no]:
  field_id == 0      →  未出征
  finished_at != ""  →  出征中，finished_at 为预计回收时间
"""

import os
from datetime import datetime
from dataclasses import dataclass
from loguru import logger

from .client import ToukenClient
from .battle import recover_sword_fatigue, FATIGUE_TARGET, FATIGUE_THRESHOLD, get_party_fatigue
from .repair import run_repair_check


@dataclass
class ExpeditionStatus:
    party_no: int
    is_active: bool          # 是否在出征中
    field_id: int            # 远征地图 ID（0=未出征）
    finished_at: str         # 预计完成时间，"" 表示未出征


def parse_expedition_status(game_state: dict) -> list[ExpeditionStatus]:
    """从 home/index 响应中解析所有部隊的远征状态"""
    conquest = game_state.get("situation", {}).get("conquest", {})
    result = []
    for party_no_str, info in conquest.items():
        party_no = int(party_no_str)
        field_id = info.get("field_id", 0)
        finished_at = info.get("finished_at", "")
        result.append(ExpeditionStatus(
            party_no=party_no,
            is_active=field_id != 0,
            field_id=field_id,
            finished_at=finished_at,
        ))
    return sorted(result, key=lambda x: x.party_no)


def is_ready_to_collect(status: ExpeditionStatus) -> bool:
    """判断远征是否已完成（可以回收）"""
    if not status.is_active or not status.finished_at:
        return False
    finished = datetime.strptime(status.finished_at, "%Y-%m-%d %H:%M:%S")
    return datetime.now() >= finished


# ── 以下方法需要抓包确认 endpoint ──────────────────────────

def send_expedition(client: ToukenClient, party_no: int, field_id: int) -> dict:
    """
    出征。
    endpoint 已通过抓包确认：conquest/start
    参数：
      party_no  —— 部隊编号（1-5）
      field_id  —— 远征地图 ID（例：3-2 = field_id 10）
    响应：无 JSON body，新 token 从 Set-Cookie 获取
    """
    logger.info(f"出征：部隊{party_no} → 地图{field_id}")
    return client._post("conquest/start", extra={
        "party_no": party_no,
        "field_id": field_id,
    })


# 各部隊出征目标配置：party_no → field_id
# 队1（白山吉光专属治疗队）不参与远征，不在此配置中
# 格式：部队号:地图ID，逗号分隔（例：2:8,4:17,5:18）
_DEFAULT_TARGETS = "2:8,3:14,4:17,5:18"

def _parse_expedition_targets(raw: str) -> dict[int, int]:
    targets = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if ":" in pair:
            party, field = pair.split(":", 1)
            targets[int(party)] = int(field)
    return targets

EXPEDITION_TARGETS: dict[int, int] = _parse_expedition_targets(
    os.getenv("EXPEDITION_TARGETS", _DEFAULT_TARGETS)
)

# 只在回收后才重新发出（登录时空闲不自动出征）
RESEND_ONLY_PARTIES: set[int] = {3}

MAP_NAMES: dict[int, str] = {
    8:  "2-4",
    10: "3-2",
    12: "3-4",
    14: "4-2",
    17: "5-1",
    18: "5-2",
}



def check_party_fatigue(conquest_data: dict, party_no: int) -> list[int]:
    """
    检查指定队伍中气力 < FATIGUE_THRESHOLD 的刀的 serial_id 列表。
    返回空列表表示全部正常。
    """
    parties = conquest_data.get("party", {})
    party_info = parties.get(str(party_no), {})
    fatigue_map = get_party_fatigue(conquest_data, party_info)
    low = [sid for sid, fat in fatigue_map.items() if fat < FATIGUE_THRESHOLD]
    if low:
        for sid in low:
            fat = fatigue_map[sid]
            logger.warning(f"  部隊{party_no} 中 serial_id={sid} 气力不足：{fat}/100（需要≥{FATIGUE_THRESHOLD}）")
    return low


def run_expedition_cycle(client: ToukenClient, state: dict) -> None:
    """
    完整远征循环：
      1. 解析当前所有部隊状态
      2. 回收已完成的远征
      3. 发出可出征的部隊：
         - 队2、4、5：空闲即发
         - 队3（RESEND_ONLY_PARTIES）：只在本次回收后重新发出，
           登录时若为空闲则跳过（避免占用常用队伍）
    """
    expeditions = parse_expedition_status(state)
    collected_parties: set[int] = set()

    for exp in expeditions:
        if not exp.is_active:
            logger.info(f"部隊{exp.party_no}: 空闲")
            continue
        if is_ready_to_collect(exp):
            logger.info(f"部隊{exp.party_no}: 已完成（{exp.finished_at}），回收中...")
            collect_expedition(client, exp.party_no)
            collected_parties.add(exp.party_no)
        else:
            logger.info(f"部隊{exp.party_no}: 出征中 → 预计 {exp.finished_at} 完成")

    if collected_parties:
        logger.info(f"共回收 {len(collected_parties)} 支部隊")
        # 回收后重新获取状态，使 is_active 更新
        state = client.get_game_state()
        expeditions = parse_expedition_status(state)
    else:
        logger.info("暂无可回收的远征")

    # 获取包含气力数据的 conquest 页数据
    conquest_data = client.get_conquest_data()

    for party_no, field_id in EXPEDITION_TARGETS.items():
        exp = next((e for e in expeditions if e.party_no == party_no), None)
        if exp and not exp.is_active:
            # 队2/3/4：仅在本次刚回收后才重新发出
            if party_no in RESEND_ONLY_PARTIES and party_no not in collected_parties:
                logger.info(f"部隊{party_no} 空闲但属于手动队，跳过自动出征")
                continue

            # 出征前检查重伤（优先于气力）
            run_repair_check(client, party_no)

            # 出征前检查气力
            conquest_data = client.get_conquest_data()
            low_fatigue = check_party_fatigue(conquest_data, party_no)
            if low_fatigue:
                logger.info(
                    f"部隊{party_no} 有 {len(low_fatigue)} 把刀气力不足，"
                    f"先刷1-1恢复气力..."
                )
                recover_sword_fatigue(client, party_no, low_fatigue)
                # 恢复后重新检查
                conquest_data = client.get_conquest_data()
                still_low = check_party_fatigue(conquest_data, party_no)
                if still_low:
                    logger.warning(f"部隊{party_no} 气力仍不足，跳过本次出征")
                    continue

            map_name = MAP_NAMES.get(field_id, str(field_id))
            logger.info(f"部隊{party_no} 空闲，气力检查通过，发往地图 {map_name}...")
            send_expedition(client, party_no=party_no, field_id=field_id)
            logger.info(f"部隊{party_no} 出征成功！")
        elif exp and exp.is_active:
            logger.info(f"部隊{party_no} 已在出征中（{exp.finished_at}），跳过")


def quick_expedition_check(client: ToukenClient) -> None:
    """
    轻量远征检查：每场战斗结束后调用。
    - 获取最新游戏状态
    - 回收所有已完成的远征，并立即按 EXPEDITION_TARGETS 重新发出
    - 不做重伤/气力检查（已在首次出征前处理过）
    """
    state = client.get_game_state()
    expeditions = parse_expedition_status(state)

    for exp in expeditions:
        if not exp.is_active or not is_ready_to_collect(exp):
            continue
        logger.info(f"[快速检查] 部隊{exp.party_no} 远征已完成，立即回收并重发")
        collect_expedition(client, exp.party_no)
        field_id = EXPEDITION_TARGETS.get(exp.party_no)
        if field_id is None:
            continue
        map_name = MAP_NAMES.get(field_id, str(field_id))
        logger.info(f"[快速检查] 部隊{exp.party_no} → {map_name}")
        send_expedition(client, party_no=exp.party_no, field_id=field_id)


def collect_expedition(client: ToukenClient, party_no: int) -> dict:
    """
    回收远征。
    endpoint 已通过抓包确认：conquest/complete
    参数：
      party_no  —— 部隊编号（1-5），不需要 field_id
    响应：完整 JSON，success=1（普通）或 2（大成功），token 在 JSON["t"]
    """
    logger.info(f"回收远征：部隊{party_no}")
    result = client._post("conquest/complete", extra={"party_no": party_no})
    success_text = "大成功！" if result.get("success") == 2 else "成功"
    logger.info(f"部隊{party_no} 远征回收 {success_text}，"
                f"获得资源：木炭+{result.get('resource', {}).get('charcoal', '?')}")
    return result

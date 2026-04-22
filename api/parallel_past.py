"""
异去（平行过去）模块

流程（每次出阵）：
  1. sally                       — 进入出阵地图；响应 JSON 若含 script 则触发日常对话
  2. scene/reproduce（可选）      — 播放对话（scene_id 从 sally 响应解析）
     scene/save  （可选）         — 确认对话
  3. sally/parallelpastsally     — 选队出阵
     * episode_id=1, field_id=4, party_no=3, item_id=0
     * status != 0               → 今日次数已用完，停止
  4. sally/parallelpaststartup   — 无额外参数，获取路线 history
  5. 循环 sally/parallelpastforward（无额外参数）：
     * scout 字段存在             → battle/battle（formation_id 来自 scout.formation_id）
     * is_finish=true            → 本次出阵结束
  重复，每日最多 PARALLEL_PAST_DAILY_MAX 次。

配置：
  PARALLEL_PAST_PARTY_NO    = 3
  PARALLEL_PAST_EPISODE_ID  = 1
  PARALLEL_PAST_FIELD_ID    = 4
  PARALLEL_PAST_DAILY_MAX   = 3
"""

import time

from loguru import logger

from .client import ToukenClient
from .battle import battle_sleep, FORMATION_ID
from .repair import run_repair_check, is_heavily_injured

PARALLEL_PAST_PARTY_NO:   int = 3
PARALLEL_PAST_EPISODE_ID: int = 1
PARALLEL_PAST_FIELD_ID:   int = 4
PARALLEL_PAST_DAILY_MAX:  int = 3


def _extract_scene_id(sally_resp: dict) -> int | None:
    """
    从 sally 响应的 script.objects 中提取 scene_id。
    voice 文件名格式：swr_voice_<scene_id>_<sword_id>_<no>
    """
    objects = sally_resp.get("script", {}).get("objects", [])
    for obj in objects:
        if obj.get("type") == "voice":
            parts = obj.get("file_name", "").split("_")
            if len(parts) >= 3:
                try:
                    return int(parts[2])
                except ValueError:
                    pass
    return None


def _run_single_parallel_past(client: ToukenClient) -> bool:
    """
    执行一次异去出阵。
    返回 True 表示正常完成；False 表示今日次数已用完。
    """
    # 1. 进入出阵地图
    sally_resp = client._post("sally")
    battle_sleep()

    # 2. 日常对话（仅 sally 响应含 script 时）
    scene_id = _extract_scene_id(sally_resp)
    if scene_id is not None:
        logger.debug(f"  检测到日常对话（scene_id={scene_id}），播放...")
        client._post("scene/reproduce", extra={"scene_id": scene_id})
        battle_sleep()
        client._post("scene/save", extra={"scene_id": scene_id})
        battle_sleep()

    # 3. 选队出阵
    sally_resp2 = client._post("sally/parallelpastsally", extra={
        "episode_id": PARALLEL_PAST_EPISODE_ID,
        "field_id":   PARALLEL_PAST_FIELD_ID,
        "party_no":   PARALLEL_PAST_PARTY_NO,
        "item_id":    0,
    })
    battle_sleep()

    if sally_resp2.get("status", 0) != 0:
        logger.info(f"  parallelpastsally status={sally_resp2.get('status')}，今日次数已用完")
        return False

    # 4. 获取路线
    startup_resp = client._post("sally/parallelpaststartup")
    history = startup_resp.get("history", [])
    logger.info(f"  路线节点数：{len(history)}")
    battle_sleep()

    # 5. 战斗循环
    battle_no = 0
    node_no = 0
    while True:
        forward_resp = client._post("sally/parallelpastforward")
        battle_sleep()

        forward_status = forward_resp.get("status", 0)
        is_finish      = forward_resp.get("is_finish", False)
        scout          = forward_resp.get("scout")
        node_no += 1

        # 队长重伤时服务器返回 status≠0，无法继续出阵
        if forward_status != 0:
            logger.warning(
                f"    节点 {node_no}：parallelpastforward status={forward_status}，"
                f"停止出阵（队长重伤）"
            )
            break

        if scout:
            formation_id = scout.get("formation_id") or FORMATION_ID
            battle_no += 1
            logger.debug(f"    节点 {node_no}：战斗 {battle_no}（formation={formation_id}）is_finish={is_finish}")
            client._post("battle/battle", extra={"formation_id": formation_id})
            battle_sleep()

            # 道中重伤检查：非最终节点战斗后，有重伤则立刻停止（防止碎刀）
            if not is_finish:
                pi = client._post("party/getpartyinfo", extra={"party_no": PARALLEL_PAST_PARTY_NO})
                injured = [str_sid for str_sid, sd in pi.get("sword", {}).items() if is_heavily_injured(sd)]
                if injured:
                    logger.opt(colors=True).warning(
                        f"<red>    节点 {node_no} 后检测到道中重伤（serial_id={injured}），停止出阵</red>"
                    )
                    break
        else:
            logger.debug(f"    节点 {node_no}：非战斗节点，跳过")

        if is_finish:
            logger.info(f"  异去出阵完成（{battle_no} 场战斗，共 {node_no} 个节点）")
            break

    # 战斗结束后游戏自动返回出阵地图，需要发送 sally 确认回到地图页
    client._post("sally")
    battle_sleep()

    return True


def run_parallel_past(client: ToukenClient) -> None:
    """
    每日异去：最多执行 PARALLEL_PAST_DAILY_MAX 次。
    服务端次数耗尽时提前结束。
    """
    logger.info(f"异去开始（每日最多 {PARALLEL_PAST_DAILY_MAX} 次，队伍{PARALLEL_PAST_PARTY_NO}）")
    completed = 0

    for run_no in range(1, PARALLEL_PAST_DAILY_MAX + 1):
        logger.info(f"[异去 第{run_no}次]")
        ok = _run_single_parallel_past(client)
        if not ok:
            break
        completed += 1

    logger.info(f"异去结束，共完成 {completed} 次")


def run_parallel_past_loop(client: ToukenClient) -> None:
    """
    异去无限循环：反复出阵直到服务端返回次数耗尽。
    适用于活动期间购买了额外次数的场景。
    每 CHECK_INTERVAL 次检查远征。按 Ctrl+C 手动停止。
    """
    import datetime
    from .expedition import run_expedition_cycle
    from .sortie import quick_expedition_check

    CHECK_INTERVAL = 10
    total_runs = 0
    start_time = datetime.datetime.now()

    logger.info(f"异去循环开始（队伍{PARALLEL_PAST_PARTY_NO}），每 {CHECK_INTERVAL} 次检查远征")

    from .sortie import adjust_captain_for_fatigue, _check_and_recover_fatigue

    try:
        while True:
            # 出阵前气力+重伤检查
            adjust_captain_for_fatigue(client, PARALLEL_PAST_PARTY_NO)
            run_repair_check(client, PARALLEL_PAST_PARTY_NO)

            total_runs += 1
            logger.info(f"[异去循环 第{total_runs}次]")

            ok = _run_single_parallel_past(client)
            if not ok:
                logger.info("服务端返回次数耗尽，循环结束")
                break

            _check_and_recover_fatigue(client, PARALLEL_PAST_PARTY_NO)
            quick_expedition_check(client)

            if total_runs % CHECK_INTERVAL == 0:
                logger.info(f"[第 {total_runs} 次] 休息 5s...")
                time.sleep(5)

    except KeyboardInterrupt:
        elapsed = datetime.datetime.now() - start_time
        logger.info(f"异去循环中断 — 共 {total_runs} 次，耗时 {str(elapsed).split('.')[0]}")
        raise

    elapsed = datetime.datetime.now() - start_time
    logger.info(f"异去循环完成 — 共 {total_runs} 次，耗时 {str(elapsed).split('.')[0]}")

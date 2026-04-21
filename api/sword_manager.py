"""
刀剑综合管理 —— 习合 + 刀解 一体化

流程：
  1. 先处理库存中已有的未保护刀（刀解释放刀位 + 习合保护刀）
  2. 领取短刀 → 习合到乱舞7
  3. 领取非短刀 → 刀解（遇到习合素材先喂保护刀）
  4. 刀位满时自动刀解腾位再继续领取
  5. 全部处理完毕 → 结束
"""

from loguru import logger

from .client import ToukenClient
from .battle import battle_sleep
from .composition import (
    _load_tantou_db, _save_tantou_db, _receive_tantou,
    _get_composition_data, _composition_loop, _print_summary,
)
from .dismantle import (
    _receive_non_tantou, _get_forge_data, _classify_swords,
    _do_dismantle, _do_composition_for_protected,
    MAX_DISMANTLE_PER_BATCH,
)


def _dismantle_existing(client: ToukenClient, tantou_sword_ids: set[int]) -> tuple[int, int]:
    """
    刀解库存中已有的可刀解刀剑（不领取，只处理现有的）。
    返回 (刀解数量, 习合数量)。
    """
    forge_data = _get_forge_data(client)
    dismantleable, comp_targets = _classify_swords(forge_data, tantou_sword_ids)

    total_dismantled = 0
    total_composed = 0

    # 先习合
    if comp_targets:
        fed = _do_composition_for_protected(client, comp_targets)
        total_composed += fed
        if fed > 0:
            logger.info(f"  库存习合：喂了 {fed} 把素材给保护刀")
            forge_data = _get_forge_data(client)
            dismantleable, _ = _classify_swords(forge_data, tantou_sword_ids)

    # 再刀解
    if dismantleable:
        logger.info(f"  库存刀解：{len(dismantleable)} 把")
        for i in range(0, len(dismantleable), MAX_DISMANTLE_PER_BATCH):
            batch = dismantleable[i:i + MAX_DISMANTLE_PER_BATCH]
            _do_dismantle(client, batch)
            total_dismantled += len(batch)

    return total_dismantled, total_composed


def run_sword_manager(client: ToukenClient) -> None:
    """
    综合管理：习合 + 刀解一体化。
    刀位满时自动刀解腾位再继续。
    """
    logger.info("刀剑综合管理开始")

    tantou_sword_ids = _load_tantou_db()
    total_dismantled = 0
    total_composed = 0

    try:
        # 第一步：处理库存中已有的未保护刀（腾刀位）
        freed, composed = _dismantle_existing(client, tantou_sword_ids)
        total_dismantled += freed
        total_composed += composed
        if freed or composed:
            logger.info(f"库存清理完成，刀解 {freed} 把，习合 {composed} 次")

        tantou_exhausted = False  # 短刀领完后不再反复检查

        while True:
            did_anything = False

            # 第二步：领取+习合短刀
            if tantou_exhausted:
                received_count, new_sword_ids = 0, set()
            else:
                received_count, new_sword_ids = _receive_tantou(client)
                if received_count == 0 and not new_sword_ids:
                    tantou_exhausted = True
                    logger.info("短刀已领完，后续跳过短刀检查")
            if new_sword_ids - tantou_sword_ids:
                tantou_sword_ids.update(new_sword_ids)
                _save_tantou_db(tantou_sword_ids)

            if received_count > 0:
                did_anything = True
                from .composition import TARGET_RANBU_LEVEL, _feed_target, mark_protected_composed
                from .dismantle import get_sword_family
                need_refresh = True

                for sword_id in tantou_sword_ids:
                    # 优先级1：保护短刀喂到乱舞10
                    if need_refresh:
                        comp_data = _get_composition_data(client)
                        need_refresh = False
                    duty_sids = set(comp_data.get("duty", []))
                    protected_targets = [
                        s for s in comp_data.get("sword", {}).values()
                        if s.get("sword_id") == sword_id
                        and s.get("protect", 0) >= 1
                        and s.get("ranbu_level", 0) < 10
                    ]
                    for pt in protected_targets:
                        if need_refresh:
                            comp_data = _get_composition_data(client)
                            need_refresh = False
                            duty_sids = set(comp_data.get("duty", []))
                        # 極化保护刀也接受未極化素材
                        compat_ids = {sword_id}
                        if pt.get("kaika_level", 0) >= 1:
                            compat_ids.add(sword_id - 1)
                        materials = [
                            s for s in comp_data.get("sword", {}).values()
                            if s.get("sword_id") in compat_ids
                            and s.get("protect", 0) == 0
                            and s.get("role_id", 0) == 0
                            and s.get("serial_id") not in duty_sids
                        ]
                        if not materials:
                            break
                        mark_protected_composed(pt["serial_id"])
                        if _feed_target(client, pt, materials, 10):
                            total_composed += 1
                            need_refresh = True

                    # 优先级2：未保护短刀互喂到乱舞7
                    while True:
                        if need_refresh:
                            comp_data = _get_composition_data(client)
                            need_refresh = False
                        duty_sids = set(comp_data.get("duty", []))
                        all_unprotected = [
                            s for s in comp_data.get("sword", {}).values()
                            if s.get("sword_id") == sword_id
                            and s.get("protect", 1) == 0
                            and s.get("role_id", 0) == 0
                            and s.get("serial_id") not in duty_sids
                        ]
                        targets = [s for s in all_unprotected if s.get("ranbu_level", 0) < TARGET_RANBU_LEVEL]
                        if not targets:
                            break
                        targets.sort(key=lambda s: (s.get("ranbu_level", 0), s.get("ranbu_exp", 0)), reverse=True)
                        target = targets[0]
                        materials = [s for s in all_unprotected if s["serial_id"] != target["serial_id"]]
                        if not materials:
                            break
                        if _feed_target(client, target, materials, TARGET_RANBU_LEVEL):
                            total_composed += 1
                            need_refresh = True
                        else:
                            break

            # 第三步：领取+刀解非短刀
            non_tantou_received = _receive_non_tantou(client)
            if non_tantou_received > 0:
                did_anything = True

            # 处理非短刀（刀解+习合保护刀）
            forge_data = _get_forge_data(client)
            dismantleable, comp_targets = _classify_swords(forge_data, tantou_sword_ids)

            if comp_targets:
                fed = _do_composition_for_protected(client, comp_targets)
                total_composed += fed
                if fed > 0:
                    forge_data = _get_forge_data(client)
                    dismantleable, _ = _classify_swords(forge_data, tantou_sword_ids)

            if dismantleable:
                did_anything = True
                for i in range(0, len(dismantleable), MAX_DISMANTLE_PER_BATCH):
                    batch = dismantleable[i:i + MAX_DISMANTLE_PER_BATCH]
                    _do_dismantle(client, batch)
                    total_dismantled += len(batch)

            # 第四步：如果领取失败（刀位满），尝试刀解腾位
            if received_count == 0 and non_tantou_received == 0:
                freed, composed = _dismantle_existing(client, tantou_sword_ids)
                total_dismantled += freed
                total_composed += composed
                if freed > 0 or composed > 0:
                    logger.info(f"刀位满，刀解 {freed} 把+习合 {composed} 次腾位，继续领取...")
                    continue
                else:
                    logger.info("无刀可领取，无刀可刀解，循环结束")
                    break

            if not did_anything:
                logger.info("本轮无操作，循环结束")
                break

    except KeyboardInterrupt:
        logger.info(f"综合管理中断 — 刀解 {total_dismantled} 把，习合 {total_composed} 次")
        _print_summary(client, tantou_sword_ids, "习合汇总")
        raise

    logger.info(f"综合管理完成 — 刀解 {total_dismantled} 把，习合 {total_composed} 次")
    _print_summary(client, tantou_sword_ids, "习合汇总")

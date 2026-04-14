"""
习合模块 —— 自动领取短刀 + 习合到乱舞7

流程：
  1. receive/list (sword_type=1) → 打开受取箱筛选短刀
  2. receive/get (serial_ids) → 领取短刀（最多100把/次）
  3. composition/index → 获取所有刀剑数据
  4. 按 sword_id 分组 protect==0 的短刀
  5. 对每组：选 ranbu_level 最高的作为目标，其余作为素材
  6. composition/union → 喂素材（最多30把/次）
  7. 到乱舞7后换同组下一个继续
  8. 素材用完回1继续领，领不到+无素材 → 结束

习合值：
  - 每把同刀 +100 ranbu_exp
  - ★1 短刀到 Lv7 累计 57 把（5700 exp）
  - ★2 短刀到 Lv7 累计 52 把（5200 exp）

配置：
  TARGET_RANBU_LEVEL = 7
  MAX_MATERIAL_PER_UNION = 30
  MAX_RECEIVE_PER_BATCH = 100
"""

import json
from pathlib import Path

from loguru import logger

from .client import ToukenClient
from .battle import battle_sleep

# 短刀 sword_id 持久化文件
_TANTOU_DB_PATH = Path(__file__).parent.parent / "data" / "tantou_sword_ids.json"

TARGET_RANBU_LEVEL: int = 7
MAX_MATERIAL_PER_UNION: int = 30
MAX_RECEIVE_PER_BATCH: int = 100
RANBU_EXP_PER_SWORD: int = 100


_tantou_names: dict[int, str] = {}  # sword_id → 刀名（全局缓存）


def _load_tantou_db() -> set[int]:
    """从本地文件加载已知短刀 sword_id 集合（兼容旧格式）"""
    global _tantou_names
    if _TANTOU_DB_PATH.exists():
        try:
            data = json.loads(_TANTOU_DB_PATH.read_text())
            if isinstance(data, dict):
                # 新格式：{sword_id_str: name}
                _tantou_names = {int(k): v for k, v in data.items()}
                ids = set(_tantou_names.keys())
            else:
                # 旧格式：[sword_id, ...]
                ids = set(data)
            logger.debug(f"加载已知短刀类型：{len(ids)} 种")
            return ids
        except (json.JSONDecodeError, TypeError):
            pass
    return set()


def _save_tantou_db(sword_ids: set[int]) -> None:
    """持久化已知短刀 {sword_id: name} 映射"""
    _TANTOU_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 保留已有名字，新的暂时用空字符串
    for sid in sword_ids:
        if sid not in _tantou_names:
            _tantou_names[sid] = ""
    data = {str(k): v for k, v in _tantou_names.items() if k in sword_ids}
    _TANTOU_DB_PATH.write_text(json.dumps(data, ensure_ascii=False))
    logger.debug(f"保存已知短刀类型：{len(sword_ids)} 种")


def get_sword_name(sword_id: int) -> str:
    """获取刀名，未知则返回 sword_id"""
    name = _tantou_names.get(sword_id, "")
    return name if name else str(sword_id)

# 乱舞等级经验值表（累计值，按稀有度）
# ranbu_level → 到达该等级需要的累计 ranbu_exp
RANBU_EXP_TABLE: dict[int, dict[int, int]] = {
    # rarity: {level: cumulative_exp}
    1: {1: 0, 2: 100, 3: 900, 4: 1700, 5: 2500, 6: 4100, 7: 5700, 8: 7300, 9: 9200, 10: 11100},
    2: {1: 0, 2: 100, 3: 800, 4: 1500, 5: 2200, 6: 3700, 7: 5200, 8: 6700, 9: 8500, 10: 10300},
    3: {1: 0, 2: 100, 3: 600, 4: 1100, 5: 1600, 6: 2700, 7: 3800, 8: 4900, 9: 6200, 10: 7500},
    4: {1: 0, 2: 100, 3: 400, 4: 700, 5: 1000, 6: 1700, 7: 2400, 8: 3100, 9: 4100, 10: 5100},
    5: {1: 0, 2: 100, 3: 300, 4: 500, 5: 700, 6: 1200, 7: 1700, 8: 2200, 9: 3000, 10: 3800},
}

# 短刀 sword_type=1 对应的 rarity 范围（大多为 ★1，部分 ★2）
# 实际 rarity 从 composition/index 响应读取


def _receive_tantou(client: ToukenClient) -> tuple[int, set[int]]:
    """
    从受取箱领取短刀。
    1. receive/list 筛选短刀，从 JSON 响应提取 serial_ids 和 sword_ids
    2. receive/get 传入 serial_ids 领取（最多100把）
    返回 (领取数量, 短刀 sword_id 集合)。
    """
    # 筛选短刀，获取受取箱列表
    list_resp = client._post("receive/list", extra={
        "sort_type": 2,
        "order": 2,
        "item_type": 0,
        "sword_type": 1,
    })
    battle_sleep()

    # 从 receive 字段提取 serial_ids 和 sword_ids
    receive_items = list_resp.get("receive", {})
    if not receive_items:
        logger.info("受取箱无短刀可领取")
        return 0, set()

    # 收集短刀 sword_id 集合 + 名字（item_id = sword_id）
    tantou_sword_ids: set[int] = set()
    for item in receive_items.values():
        sid = item["item_id"]
        tantou_sword_ids.add(sid)
        name = item.get("name", "")
        if name and (sid not in _tantou_names or not _tantou_names[sid]):
            _tantou_names[sid] = name

    # 取前100个
    all_sids = list(receive_items.keys())
    batch_sids = all_sids[:MAX_RECEIVE_PER_BATCH]

    logger.info(f"受取箱显示 {len(all_sids)} 把短刀（{len(tantou_sword_ids)} 种），本次领取 {len(batch_sids)} 把")

    # 领取
    serial_ids_str = ",".join(batch_sids)
    resp = client._post("receive/get", extra={
        "serial_ids": serial_ids_str,
        "sort_type": 2,
        "order": 2,
        "item_type": 0,
        "sword_type": 1,
    })
    battle_sleep()

    received = resp.get("serial_ids", [])
    received_count = len(received) if received else 0
    if received_count:
        logger.info(f"成功领取 {received_count} 把短刀")
    else:
        logger.warning("receive/get 返回空，领取可能失败（刀位满？）")

    return received_count, tantou_sword_ids


def _get_composition_data(client: ToukenClient) -> dict:
    """调用 composition/index 获取所有刀剑数据"""
    resp = client._post("composition/index")
    battle_sleep()
    return resp.get("composition", resp)


def _find_tantou_groups(composition_data: dict) -> dict[int, list[dict]]:
    """
    从 composition 数据中找出所有 protect==0 的短刀，按 sword_id 分组。
    只包含 rarity <= 2 的短刀（短刀稀有度一般为 ★1 或 ★2）。
    返回 {sword_id: [sword_data, ...]}，每组按 ranbu_level 降序排列。
    """
    swords = composition_data.get("sword", {})
    groups: dict[int, list[dict]] = {}

    for sid_str, sword in swords.items():
        # 只要 protect==0 且 ranbu_level < 满级的短刀
        if sword.get("protect", 1) != 0:
            continue
        # 短刀 rarity 一般为 1 或 2（composition/index 没有 sword_type 字段，
        # 但 rarity <= 2 基本就是短刀/�的比较低的刀）
        # 实际判断：用从 receive/get 领取的 serial_id 来确定哪些是短刀
        sword_id = sword.get("sword_id")
        if sword_id is None:
            continue
        groups.setdefault(sword_id, []).append(sword)

    # 每组按 ranbu_level 降序，同等级按 ranbu_exp 降序
    for sword_id in groups:
        groups[sword_id].sort(
            key=lambda s: (s.get("ranbu_level", 0), s.get("ranbu_exp", 0)),
            reverse=True,
        )

    return groups


def _swords_needed_for_target(sword: dict, target_level: int) -> int:
    """计算一把刀到达目标乱舞等级还需要喂多少把同刀"""
    rarity = sword.get("rarity", 1)
    current_exp = sword.get("ranbu_exp", 0)
    exp_table = RANBU_EXP_TABLE.get(rarity, RANBU_EXP_TABLE[1])
    target_exp = exp_table.get(target_level, 5700)
    remaining = target_exp - current_exp
    if remaining <= 0:
        return 0
    return (remaining + RANBU_EXP_PER_SWORD - 1) // RANBU_EXP_PER_SWORD  # 向上取整


def _do_union(client: ToukenClient, base_serial_id: int, material_serial_ids: list[int]) -> None:
    """执行一次习合"""
    material_str = ",".join(str(sid) for sid in material_serial_ids)
    client._post("composition/union", extra={
        "base_serial_id": base_serial_id,
        "material_serial_id": material_str,
    })
    battle_sleep()
    logger.info(f"  习合完成：base={base_serial_id}，素材 {len(material_serial_ids)} 把")


def _feed_target(
    client: ToukenClient,
    target: dict,
    materials: list[dict],
    target_level: int,
) -> bool:
    """
    给一个目标刀喂素材直到达到 target_level。
    返回是否执行了任何习合。
    """
    target_sid = target["serial_id"]
    target_ranbu = target.get("ranbu_level", 0)
    target_exp = target.get("ranbu_exp", 0)
    is_protected = target.get("protect", 0) >= 1

    needed = _swords_needed_for_target(target, target_level)
    if needed == 0 or not materials:
        return False

    logger.info(
        f"  目标 serial_id={target_sid}（{'保护' if is_protected else '未保护'}）"
        f" ranbu_lv={target_ranbu} exp={target_exp}，"
        f"目标等级={target_level}，还需 {needed} 把，可用素材 {len(materials)} 把"
    )

    fed_count = 0
    while materials and fed_count < needed:
        batch_size = min(MAX_MATERIAL_PER_UNION, needed - fed_count)
        batch = materials[:batch_size]
        batch_sids = [s["serial_id"] for s in batch]
        _do_union(client, target_sid, batch_sids)
        fed_count += len(batch)
        del materials[:len(batch)]

    estimated_exp = target_exp + fed_count * RANBU_EXP_PER_SWORD
    logger.info(
        f"  完成：喂了 {fed_count} 把，预估 exp={estimated_exp}"
        + (f"，已达乱舞{target_level}" if fed_count >= needed else f"，还差 {needed - fed_count} 把")
    )

    return True


def run_composition_cycle(client: ToukenClient) -> None:
    """
    自动习合循环：领取短刀 → 习合 → 领更多 → 直到无刀可领+无素材。
    Ctrl+C 中断时也打印汇总。
    """
    known_tantou_sword_ids: set[int] = _load_tantou_db()
    try:
        _composition_loop(client, known_tantou_sword_ids)
        _print_summary(client, known_tantou_sword_ids, "习合循环完成")
    except KeyboardInterrupt:
        _print_summary(client, known_tantou_sword_ids, "习合循环中断")
        raise


def _composition_loop(client: ToukenClient, known_tantou_sword_ids: set[int]) -> None:
    """习合主循环"""
    logger.info("习合循环开始")

    while True:
        # 1. 领取短刀
        received_count, new_sword_ids = _receive_tantou(client)
        if new_sword_ids - known_tantou_sword_ids:
            known_tantou_sword_ids.update(new_sword_ids)
            _save_tantou_db(known_tantou_sword_ids)

        # 2. 获取 composition 数据
        comp_data = _get_composition_data(client)
        all_swords = comp_data.get("sword", {})

        # 3. 补充：从库存中扫描已确认的短刀 sword_id（仅限 receive/list 确认过的类型）
        #    解决重启后旧遗留的半成品/Lv7+刀没被处理的问题
        #    不会误操作非短刀，因为只处理 known_tantou_sword_ids 中的类型

        tantou_sword_ids = known_tantou_sword_ids

        if not tantou_sword_ids:
            logger.info("没有已知短刀种类，结束")
            break

        # 4. 按 sword_id 分组
        #    protected_targets: protect >= 1 且 ranbu_level < 7（优先喂到10）
        #    materials: protect == 0 的（素材或备选目标）
        protected_targets: dict[int, list[dict]] = {}
        material_pool: dict[int, list[dict]] = {}

        # 不可用的刀：编队中（role_id != 0）或内番中（duty 列表）
        duty_sids = set(comp_data.get("duty", []))

        for sid_str, sword in all_swords.items():
            sword_id = sword.get("sword_id")
            if sword_id not in tantou_sword_ids:
                continue
            if sword.get("role_id", 0) != 0 or sword.get("serial_id") in duty_sids:
                continue

            if sword.get("protect", 0) >= 1 and sword.get("ranbu_level", 0) < TARGET_RANBU_LEVEL:
                protected_targets.setdefault(sword_id, []).append(sword)
            elif sword.get("protect", 0) == 0:
                material_pool.setdefault(sword_id, []).append(sword)

        # 素材按 ranbu_level 升序（低的先被喂掉）
        for sword_id in material_pool:
            material_pool[sword_id].sort(
                key=lambda s: (s.get("ranbu_level", 0), s.get("ranbu_exp", 0)),
            )

        # 5. 习合
        did_any_union = False

        # 优先级1：喂 protect 的刀到乱舞10
        for sword_id, targets in protected_targets.items():
            for target in targets:
                # 从最新数据获取素材（排除编队/内番中的刀）
                comp_data = _get_composition_data(client)
                duty_sids = set(comp_data.get("duty", []))
                materials = [
                    s for s in comp_data.get("sword", {}).values()
                    if s.get("sword_id") == sword_id
                    and s.get("protect", 1) == 0
                    and s.get("role_id", 0) == 0
                    and s.get("serial_id") not in duty_sids
                ]
                if not materials:
                    break
                if _feed_target(client, target, materials, target_level=10):
                    did_any_union = True

        # 优先级2：protect==0 的刀之间互喂到乱舞7
        for sword_id in list(material_pool.keys()):
            while True:
                # 每次从最新数据重建该 sword_id 的未保护刀列表
                comp_data = _get_composition_data(client)
                duty_sids = set(comp_data.get("duty", []))
                all_unprotected = [
                    s for s in comp_data.get("sword", {}).values()
                    if s.get("sword_id") == sword_id
                    and s.get("protect", 1) == 0
                    and s.get("role_id", 0) == 0
                    and s.get("serial_id") not in duty_sids
                ]
                # 目标：ranbu_level < 7 的，选最高的
                targets = [s for s in all_unprotected if s.get("ranbu_level", 0) < TARGET_RANBU_LEVEL]
                if not targets:
                    break
                targets.sort(
                    key=lambda s: (s.get("ranbu_level", 0), s.get("ranbu_exp", 0)),
                    reverse=True,
                )
                target = targets[0]
                # 素材：除目标外所有未保护刀（含已到Lv7的）
                target_sid = target["serial_id"]
                materials = [s for s in all_unprotected if s["serial_id"] != target_sid]
                if not materials:
                    break
                if _feed_target(client, target, materials, target_level=TARGET_RANBU_LEVEL):
                    did_any_union = True
                else:
                    break

        if not did_any_union and received_count == 0:
            # 尝试刀解非短刀腾位
            freed = _try_free_slots(client, known_tantou_sword_ids)
            if freed > 0:
                logger.info(f"刀位满，刀解 {freed} 把非短刀腾位，继续领取...")
                continue
            logger.info("无新短刀可领取，无素材可习合，无可刀解腾位，循环结束")
            break

        if not did_any_union and received_count > 0:
            logger.info("本轮领取了刀但无法习合（素材不足），继续领取...")
            continue



def _try_free_slots(client: ToukenClient, tantou_sword_ids: set[int]) -> int:
    """刀解非短刀释放刀位，返回刀解数量"""
    from .dismantle import _get_forge_data, _classify_swords, _do_dismantle, MAX_DISMANTLE_PER_BATCH
    forge_data = _get_forge_data(client)
    dismantleable, comp_targets = _classify_swords(forge_data, tantou_sword_ids)

    # 先习合保护刀的素材
    if comp_targets:
        from .dismantle import _do_composition_for_protected
        _do_composition_for_protected(client, comp_targets)
        forge_data = _get_forge_data(client)
        dismantleable, _ = _classify_swords(forge_data, tantou_sword_ids)

    freed = 0
    for i in range(0, len(dismantleable), MAX_DISMANTLE_PER_BATCH):
        batch = dismantleable[i:i + MAX_DISMANTLE_PER_BATCH]
        _do_dismantle(client, batch)
        freed += len(batch)
    return freed


def _print_summary(client: ToukenClient, sword_ids: set[int], title: str) -> None:
    """打印习合汇总"""
    try:
        comp_data = _get_composition_data(client)
    except Exception:
        logger.warning("无法获取汇总数据")
        return
    summary: dict[int, dict] = {}
    for sword in comp_data.get("sword", {}).values():
        sword_id = sword.get("sword_id")
        if sword_id not in sword_ids:
            continue
        if sword.get("protect", 0) != 0:
            continue
        if sword_id not in summary:
            summary[sword_id] = {"lv7": 0, "incomplete": []}
        if sword.get("ranbu_level", 0) >= TARGET_RANBU_LEVEL:
            summary[sword_id]["lv7"] += 1
        else:
            summary[sword_id]["incomplete"].append(sword)

    total_lv7 = sum(v["lv7"] for v in summary.values())
    total_incomplete = sum(len(v["incomplete"]) for v in summary.values())
    logger.info(f"{title} — 已达乱舞{TARGET_RANBU_LEVEL}：{total_lv7} 把，未完成：{total_incomplete} 把")
    for sid, data in summary.items():
        if not data["lv7"] and not data["incomplete"]:
            continue
        detail = ""
        if data["incomplete"]:
            # 显示未完成刀的当前等级和还差多少
            parts = []
            for s in data["incomplete"]:
                lv = s.get("ranbu_level", 0)
                needed = _swords_needed_for_target(s, TARGET_RANBU_LEVEL)
                parts.append(f"Lv{lv}(差{needed}把)")
            detail = f"，未完成：{', '.join(parts)}"
        name = get_sword_name(sid)
        logger.info(f"  {name}：Lv{TARGET_RANBU_LEVEL}+ {data['lv7']} 把{detail}")

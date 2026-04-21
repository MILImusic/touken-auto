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
  - 绝不碰 protect >= 1 的刀（不刀解保护刀，习合只是给它喂经验）
  - 同 sword_id 存在保护刀且 ranbu_level < 10 → 留给习合，不刀解
  - 不刀解编队中（role_id != 0）或内番中的刀
  - 保护刀作为习合目标：在队/内番都能喂（内番刀可以习合，只有远征不行）

配置：
  NON_TANTOU_SWORD_TYPES = "2,6,5,7,3,4,10"
  MAX_DISMANTLE_PER_BATCH = 30
  MAX_RECEIVE_PER_BATCH = 100
"""

from loguru import logger

from .client import ToukenClient
from .battle import battle_sleep
from .composition import _load_tantou_db, _do_union, _swords_needed_for_target, _get_composition_data, MAX_MATERIAL_PER_UNION, register_sword_names, mark_protected_composed

NON_TANTOU_SWORD_TYPES: str = "2,6,5,7,3,4,10"
MAX_DISMANTLE_PER_BATCH: int = 30
MAX_RECEIVE_PER_BATCH: int = 100

# 多形态刀剣家族（形態変化 / 特，不走标准極化的 +1 规则）
# 同一家族的所有 sword_id 视为"同一把刀"
_SWORD_FAMILIES: list[set[int]] = [
    {107, 108, 109, 110, 111},  # 髭切: base → 特1 → 特2 → 特3 → 極
    {112, 113, 114, 115},       # 膝丸: base → 特1 → 特2 → 極
]
_FAMILY_MAP: dict[int, set[int]] = {}
for _fam in _SWORD_FAMILIES:
    for _sid in _fam:
        _FAMILY_MAP[_sid] = _fam


def get_sword_family(sword_id: int, kaika_level: int = 0) -> set[int]:
    """获取同一刀剣的所有形态 ID（含極化+1）"""
    if sword_id in _FAMILY_MAP:
        return _FAMILY_MAP[sword_id]
    # 標準極化：kaika_level>=1 或名字以「・極」结尾（有些極化刀 kaika_level=0）
    is_kiwame = kaika_level >= 1
    if not is_kiwame:
        from .composition import get_sword_name
        name = get_sword_name(sword_id)
        if name.endswith("・極"):
            is_kiwame = True
    if is_kiwame:
        return {sword_id - 1, sword_id}  # 極化刀 → 往回找原始
    return {sword_id, sword_id + 1}      # 原始刀 → 往前找極化


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

    # 采集刀名
    names = {item["item_id"]: item.get("name", "") for item in receive_items.values() if item.get("name")}
    register_sword_names(names)

    all_sids = list(receive_items.keys())
    batch_sids = all_sids[:MAX_RECEIVE_PER_BATCH]

    logger.info(f"受取箱显示 {len(all_sids)} 把非短刀，本次领取 {len(batch_sids)} 把")

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


def _classify_swords(forge_data: dict, tantou_sword_ids: set[int]) -> tuple[list[int], dict[int, list[dict]]]:
    """
    从 forge 数据中分类未保护刀剑。

    返回：(dismantleable_sids, composition_targets)
      - dismantleable_sids: 可刀解的 serial_id 列表
      - composition_targets: {protected_serial_id: [material_sword_data, ...]}
        保护刀 serial_id → 可喂给它的素材刀数据列表
    """
    swords = forge_data.get("sword", {})

    # 提取内番中的 serial_id
    # forge 格式：{type, horse_id1/2, field_id1/2, bout_id1/2, finished_at}
    # composition 格式：[serial_id, ...]
    duty = forge_data.get("duty", {})
    duty_sids: set[int] = set()
    if isinstance(duty, list):
        duty_sids = set(duty)
    elif isinstance(duty, dict):
        for key in ("horse_id1", "horse_id2", "field_id1", "field_id2", "bout_id1", "bout_id2"):
            val = duty.get(key)
            if isinstance(val, int):
                duty_sids.add(val)

    # 构建保护刀映射：sword_id → {serial_id, ranbu_level, rarity, ranbu_exp, ...}
    # 極化刀同时映射原始 sword_id（-1），让未極化素材也能匹配
    protected_map: dict[int, dict] = {}  # sword_id → 保护刀数据
    # 先检查 forge 数据是否有 kaika_level 字段
    _sample = next(iter(swords.values()), {}) if swords else {}
    has_kaika = "kaika_level" in _sample
    if not has_kaika and swords:
        logger.warning("forge 数据无 kaika_level 字段，極化刀映射可能失效")

    for sword in swords.values():
        if sword.get("protect", 0) >= 1:
            sid = sword.get("sword_id")
            # 取 ranbu_level 最低的保护刀（最需要喂的）
            if sid not in protected_map or sword.get("ranbu_level", 0) < protected_map[sid].get("ranbu_level", 10):
                protected_map[sid] = sword
            # 同家族所有形态 ID 都映射到这把保护刀，让任意形态的素材都能匹配
            for family_id in get_sword_family(sid, sword.get("kaika_level", 0)):
                if family_id != sid:
                    if family_id not in protected_map or sword.get("ranbu_level", 0) < protected_map[family_id].get("ranbu_level", 10):
                        protected_map[family_id] = sword

    # 保护刀数量简要提示（不再逐条打印）
    unfed = {sid: s for sid, s in protected_map.items() if s.get("ranbu_level", 0) < 10}
    if unfed:
        logger.debug(f"保护刀映射：{len(protected_map)} 条（其中 {len(unfed)} 条 ranbu<10）")

    # 构建「有保护版本」的 sword_id 集合（含家族所有形态）
    has_protected: set[int] = set()
    for sword in swords.values():
        if sword.get("protect", 0) >= 1:
            has_protected.update(get_sword_family(sword.get("sword_id"), sword.get("kaika_level", 0)))

    # 分类
    dismantleable: list[int] = []
    comp_materials: dict[int, list[dict]] = {}  # protected_serial_id → [material_data]
    kept_new: set[int] = set()  # 已保留一把的新刀 sword_id

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

        # 有保护刀且 ranbu_level < 10 → 习合素材
        if sword_id in protected_map and protected_map[sword_id].get("ranbu_level", 0) < 10:
            target_sid = protected_map[sword_id]["serial_id"]
            comp_materials.setdefault(target_sid, []).append(sword)
        # 没有保护版本 → 新刀，保留一把不刀解
        elif sword_id not in has_protected and sword_id not in kept_new:
            kept_new.add(sword_id)
            from .composition import get_sword_name
            logger.info(f"  新刀保留：{get_sword_name(sword_id)}(id={sword_id}) — 仓库无保护版本，跳过刀解")
        else:
            dismantleable.append(sword["serial_id"])

    if comp_materials:
        from .composition import get_sword_name
        targets_info = ", ".join(
            f"{get_sword_name(protected_map.get(next(iter([])), {}).get('sword_id', '?'))}×{len(mats)}"
            if False else f"{len(mats)}把"
            for t_sid, mats in comp_materials.items()
        )
        logger.info(f"  找到 {sum(len(m) for m in comp_materials.values())} 把素材可喂 {len(comp_materials)} 把保护刀")

    return dismantleable, comp_materials


def _do_dismantle(client: ToukenClient, serial_ids: list[int]) -> None:
    """执行一次刀解"""
    serial_id_str = ",".join(str(sid) for sid in serial_ids)
    client._post("forge/dismantle", extra={
        "serial_id": serial_id_str,
    })
    battle_sleep()
    logger.info(f"  刀解完成：{len(serial_ids)} 把")


def _do_composition_for_protected(client: ToukenClient, comp_targets: dict[int, list[dict]]) -> int:
    """
    给保护刀喂习合素材。
    返回喂掉的素材数量。
    """
    total_fed = 0
    for target_sid, materials in comp_targets.items():
        if not materials:
            continue

        # 获取保护刀最新数据
        comp_data = _get_composition_data(client)
        target = comp_data.get("sword", {}).get(str(target_sid))
        if not target:
            continue

        ranbu = target.get("ranbu_level", 0)
        if ranbu >= 10:
            continue

        # 保护刀只是接收经验不会被销毁，在队/内番都能喂
        # （内番刀可以习合，只有远征中的不行）

        needed = _swords_needed_for_target(target, 10)
        to_feed = materials[:needed]

        if not to_feed:
            continue

        mark_protected_composed(target_sid)
        logger.info(
            f"  习合：保护刀 serial_id={target_sid}"
            f"（ranbu_lv={ranbu}），喂 {len(to_feed)} 把素材"
        )

        fed = 0
        while to_feed and fed < needed:
            batch_size = min(MAX_MATERIAL_PER_UNION, needed - fed)
            batch = to_feed[:batch_size]
            batch_sids = [s["serial_id"] for s in batch]
            _do_union(client, target_sid, batch_sids)
            fed += len(batch)
            del to_feed[:len(batch)]

        total_fed += fed

    return total_fed


def run_dismantle_cycle(client: ToukenClient) -> None:
    """
    自动刀解循环：领取非短刀 → 习合保护刀 → 刀解 → 领更多 → 直到无刀可领。
    """
    logger.info("刀解循环开始")
    tantou_sword_ids = _load_tantou_db()
    total_dismantled = 0
    total_composed = 0

    try:
        while True:
            # 1. 领取非短刀
            received_count = _receive_non_tantou(client)

            # 2. 获取 forge 数据
            forge_data = _get_forge_data(client)

            # 3. 分类：可刀解 vs 习合素材
            dismantleable, comp_targets = _classify_swords(forge_data, tantou_sword_ids)

            # 4. 先习合（给保护刀喂素材）
            if comp_targets:
                fed = _do_composition_for_protected(client, comp_targets)
                total_composed += fed
                if fed > 0:
                    # 习合后素材已消耗，重新获取 forge 数据刷新可刀解列表
                    forge_data = _get_forge_data(client)
                    dismantleable, _ = _classify_swords(forge_data, tantou_sword_ids)

            # 5. 刀解
            if dismantleable:
                logger.info(f"找到 {len(dismantleable)} 把可刀解的刀")
                for i in range(0, len(dismantleable), MAX_DISMANTLE_PER_BATCH):
                    batch = dismantleable[i:i + MAX_DISMANTLE_PER_BATCH]
                    _do_dismantle(client, batch)
                    total_dismantled += len(batch)

            if not dismantleable and not comp_targets and received_count == 0:
                # 刀位可能满了，尝试处理库存中已有的可刀解刀
                forge_data2 = _get_forge_data(client)
                existing, _ = _classify_swords(forge_data2, tantou_sword_ids)
                if existing:
                    logger.info(f"刀位满，刀解库存 {len(existing)} 把腾位...")
                    for i in range(0, len(existing), MAX_DISMANTLE_PER_BATCH):
                        batch = existing[i:i + MAX_DISMANTLE_PER_BATCH]
                        _do_dismantle(client, batch)
                        total_dismantled += len(batch)
                    continue
                logger.info("无非短刀可领取，无可操作的刀，循环结束")
                break

            if not dismantleable and not comp_targets and received_count > 0:
                logger.info("本轮领取了刀但无可操作，继续领取...")
                continue

    except KeyboardInterrupt:
        logger.info(f"刀解循环中断 — 刀解 {total_dismantled} 把，习合 {total_composed} 把")
        raise

    logger.info(f"刀解循环完成 — 刀解 {total_dismantled} 把，习合 {total_composed} 把")

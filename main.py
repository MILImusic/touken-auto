"""
刀剣乱舞日服自动化 —— 日常远征管理

流程版本：
  1. 爬楼地下城日常版 —— 日常任务 + 地下城爬楼（爬至目标层）
  2. 日常任务版       —— 仅日常任务（重伤治疗 + 远征 + 演练）
  3. 循环地下城日常版 —— 日常任务 + 地下城单层循环
"""

import os
import time
from dotenv import load_dotenv
from loguru import logger

from api.client import ToukenClient
from api.expedition import run_expedition_cycle
from api.repair import run_all_repairs
from api.practice import run_practice_cycle
from api.dungeon import run_dungeon_climb, run_dungeon_floor_loop
from api.sortie import run_sortie_4_3_loop, run_sortie_4_4_loop
from api.parallel_past import run_parallel_past

load_dotenv()

UID    = os.getenv("UID", "14052501")
SERVER = os.getenv("SERVER", "w021")

MODES = {
    1: "爬楼地下城日常版（日常任务 + 地下城爬楼）",
    2: "日常任务版（重伤治疗 + 远征 + 演练 + 异去）",
    3: "循环地下城日常版（日常任务 + 地下城单层循环）",
    4: "循环4-4版（日常任务 + 4-4无限循环，每15次检查依赖札）",
    5: "循环4-3版（日常任务 + 4-3无限循环，队伍4，每15次固定休息）",
}


def get_credentials() -> tuple[str, str]:
    """
    从 DevTools Network → 最新的 reflect?uid= → Response Headers 复制两个值：
      sword          = Set-Cookie: sword=...
      fuel_csrf_token = Set-Cookie: fuel_csrf_token=...
    """
    print("\n请从 DevTools Network → 最新的 reflect → Response Headers 复制：")
    sword = input("  sword: ").strip()
    t     = input("  fuel_csrf_token: ").strip()
    if not sword or not t:
        raise ValueError("sword 和 t 不能为空")
    return sword, t


def select_mode() -> int:
    print("\n请选择运行版本：")
    for num, desc in MODES.items():
        print(f"  {num}. {desc}")
    while True:
        raw = input("输入数字（1/2/3/4/5）: ").strip()
        if raw in ("1", "2", "3", "4", "5"):
            return int(raw)
        print("  请输入 1、2、3、4 或 5")


def handle_leave_requests(client: ToukenClient, state: dict) -> None:
    """
    自动拒绝登录时等待修行的访客刀剑。
    home/index 有访客时会出现 leave 字段（列表），无访客时字段缺失。
    若字段名不对，下次有访客时看 DEBUG 日志确认实际字段名。
    """
    visitors = state.get("leave")
    if not visitors:
        logger.debug("home/index 无访客字段（或字段为空），跳过 leave 处理")
        return
    logger.info(f"检测到 {len(visitors)} 位访客，自动拒绝...")
    for v in visitors:
        serial_id = v.get("serial_id")
        if not serial_id:
            continue
        logger.info(f"  拒绝访客 serial_id={serial_id}")
        client._post("home/leave", extra={"serial_id": serial_id, "is_leave": 0})
        time.sleep(1)


def run_daily(client: ToukenClient, state: dict) -> None:
    """日常任务：重伤治疗 → 远征 → 演练 → 异去"""
    # home/index 响应的 sword 字段含准确 HP，直接传入避免额外 API 调用
    run_all_repairs(client)
    run_expedition_cycle(client, state)
    run_practice_cycle(client)
    run_parallel_past(client)


def main():
    sword, initial_t = get_credentials()
    mode = select_mode()

    # 需要层数的模式提前收集输入
    layer_id: int | None = None
    start_layer: int | None = None
    if mode == 1:
        start_layer = int(input("请输入当前地下城层数（爬楼起始层）: ").strip())
    elif mode == 3:
        layer_id = int(input("请输入循环层数（如 88）: ").strip())

    with ToukenClient(sword=sword, uid=UID, server=SERVER) as client:
        client._csrf_token = initial_t
        logger.info(f"Token 已就绪，运行模式：{MODES[mode]}")

        state = client.get_game_state()
        if state.get("status", 0) != 0:
            raise RuntimeError(
                f"home/index 返回 status={state.get('status')}，"
                "sword / token 已过期，请重新从 DevTools 复制凭证"
            )
        handle_leave_requests(client, state)
        res = state["resource"]
        logger.info(
            f"资源 — 木炭:{res['charcoal']}  玉钢:{res['steel']}  "
            f"冷却剂:{res['coolant']}  砥石:{res['file']}"
        )

        if mode == 1:
            # 爬楼地下城日常版
            run_daily(client, state)
            run_dungeon_climb(client, start_layer=start_layer)

        elif mode == 2:
            # 日常任务版
            run_daily(client, state)

        elif mode == 3:
            # 循环地下城日常版
            run_daily(client, state)
            run_dungeon_floor_loop(client, layer_id=layer_id)

        elif mode == 4:
            # 循环4-4版（重伤治疗 + 远征 + 演练 + 4-4无限循环）
            run_daily(client, state)
            run_sortie_4_4_loop(client)

        elif mode == 5:
            # 循环4-3版（重伤治疗 + 远征 + 演练 + 4-3无限循环）
            run_daily(client, state)
            run_sortie_4_3_loop(client)


def _beep(sound: str = "Basso") -> None:
    """播放 macOS 系统提示音（不阻塞，失败静默）"""
    import subprocess
    try:
        subprocess.Popen(["afplay", f"/System/Library/Sounds/{sound}.aiff"])
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("用户中断（Ctrl+C），程序退出")
    except Exception as e:
        logger.error(f"程序异常退出：{e}")
        _beep("Basso")
        raise

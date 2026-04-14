"""
刀剣乱舞日服自动化 —— 日常远征管理

流程版本：
  1. 爬楼地下城日常版 —— 日常任务 + 地下城爬楼（爬至目标层）
  2. 日常任务版       —— 仅日常任务（重伤治疗 + 远征 + 演练 + 异去）
  3. 循环地下城日常版 —— 日常任务 + 地下城单层循环
  4. 循环4-4版       —— 日常任务 + 4-4无限循环（队伍3，检查依赖札）
  5. 循环4-3版       —— 日常任务 + 4-3无限循环（队伍3）
  6. 循环7-3版       —— 日常任务 + 7-3无限循环（队伍3，跳过最终节点）
"""

import os
import time
import traceback
from dotenv import load_dotenv
from loguru import logger
import httpx

from api.client import ToukenClient
from api.expedition import run_expedition_cycle
from api.repair import run_all_repairs
from api.composition import run_composition_cycle
from api.dismantle import run_dismantle_cycle
from api.sword_manager import run_sword_manager
from api.practice import run_practice_cycle
from api.dungeon import run_dungeon_climb, run_dungeon_floor_loop
from api.sortie import run_sortie_4_3_loop, run_sortie_4_4_loop, run_sortie_7_3_loop
from api.parallel_past import run_parallel_past

load_dotenv()

UID    = os.getenv("UID", "14052501")
SERVER = os.getenv("SERVER", "w021")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = "YOUR_TELEGRAM_CHAT_ID"

# ANSI 颜色
_C = {
    "green":   "\033[32m",
    "yellow":  "\033[33m",
    "blue":    "\033[34m",
    "magenta": "\033[35m",
    "cyan":    "\033[36m",
    "reset":   "\033[0m",
    "bold":    "\033[1m",
    "dim":     "\033[2m",
}

MODES = {
    1: "爬楼地下城日常版（日常任务 + 地下城爬楼）",
    2: "日常任务版（重伤治疗 + 远征 + 演练 + 异去）",
    3: "循环地下城日常版（日常任务 + 地下城单层循环）",
    4: "循环4-4版（日常任务 + 4-4无限循环，队伍3，每15次检查依赖札）",
    5: "循环4-3版（日常任务 + 4-3无限循环，队伍3，每15次固定休息）",
    6: "循环7-3版（日常任务 + 7-3无限循环，队伍3，每15次固定休息）",
    7: "习合模式（领取短刀 + 自动习合到乱舞7）",
    8: "刀解模式（领取非短刀 + 自动刀解释放刀位）",
    9: "综合管理（习合+刀解一体化，刀位满自动腾位）",
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
    R = _C["reset"]
    print(f"\n{_C['bold']}请选择运行版本：{R}")
    print(f"  {_C['dim']}── 日常 ──{R}")
    print(f"  {_C['green']}2. {MODES[2]}{R}")
    print(f"  {_C['dim']}── 活动循环 ──{R}")
    print(f"  {_C['yellow']}1. {MODES[1]}{R}")
    print(f"  {_C['yellow']}3. {MODES[3]}{R}")
    print(f"  {_C['dim']}── 普通地图循环 ──{R}")
    print(f"  {_C['cyan']}4. {MODES[4]}{R}")
    print(f"  {_C['cyan']}5. {MODES[5]}{R}")
    print(f"  {_C['cyan']}6. {MODES[6]}{R}")
    print(f"  {_C['dim']}── 其他 ──{R}")
    print(f"  {_C['magenta']}7. {MODES[7]}{R}")
    print(f"  {_C['magenta']}8. {MODES[8]}{R}")
    print(f"  {_C['magenta']}9. {MODES[9]}{R}")
    while True:
        raw = input("输入数字（1-9）: ").strip()
        if raw in ("1", "2", "3", "4", "5", "6", "7", "8", "9"):
            return int(raw)
        print("  请输入 1～9")


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


NEXT_MODE_TIMEOUT = 300  # 模式完成后等待下一个选择的秒数


def _input_with_timeout(prompt: str, timeout: int) -> str | None:
    """带超时的 input，超时返回 None（仅 macOS/Linux）"""
    import signal

    def _handler(signum, frame):
        raise TimeoutError()

    old_handler = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(timeout)
    try:
        result = input(prompt)
        signal.alarm(0)
        return result
    except TimeoutError:
        print()
        return None
    finally:
        signal.signal(signal.SIGALRM, old_handler)
        signal.alarm(0)


_daily_done = False


def _run_mode(client: ToukenClient, mode: int, state: dict) -> None:
    """执行指定模式"""
    global _daily_done

    def _ensure_daily():
        global _daily_done
        if not _daily_done:
            run_daily(client, state)
            _daily_done = True
        else:
            logger.info("日常任务本次 session 已执行，跳过")

    if mode == 1:
        start_layer = int(input("请输入当前地下城层数（爬楼起始层）: ").strip())
        _ensure_daily()
        run_dungeon_climb(client, start_layer=start_layer)

    elif mode == 2:
        _ensure_daily()

    elif mode == 3:
        layer_id = int(input("请输入循环层数（如 88）: ").strip())
        _ensure_daily()
        run_dungeon_floor_loop(client, layer_id=layer_id)

    elif mode == 4:
        _ensure_daily()
        run_sortie_4_4_loop(client)

    elif mode == 5:
        _ensure_daily()
        run_sortie_4_3_loop(client)

    elif mode == 6:
        _ensure_daily()
        run_sortie_7_3_loop(client)

    elif mode == 7:
        run_composition_cycle(client)

    elif mode == 8:
        run_dismantle_cycle(client)

    elif mode == 9:
        run_sword_manager(client)


def main():
    sword, initial_t = get_credentials()
    mode = select_mode()

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

        while True:
            try:
                _run_mode(client, mode, state)
            except KeyboardInterrupt:
                logger.info("用户中断（Ctrl+C），当前模式结束")
            except Exception as e:
                logger.error(f"模式异常退出：{e}")
                _notify_telegram(e)

            # 模式完成，等待选择下一个
            logger.info(f"模式完成，{NEXT_MODE_TIMEOUT // 60} 分钟内可选择下一个模式，超时自动退出")
            print()
            # 复用带颜色的模式列表
            R = _C["reset"]
            print(f"{_C['bold']}可选模式：{R}")
            print(f"  {_C['dim']}── 日常 ──{R}")
            print(f"  {_C['green']}2. {MODES[2]}{R}")
            print(f"  {_C['dim']}── 活动循环 ──{R}")
            print(f"  {_C['yellow']}1. {MODES[1]}{R}")
            print(f"  {_C['yellow']}3. {MODES[3]}{R}")
            print(f"  {_C['dim']}── 普通地图循环 ──{R}")
            print(f"  {_C['cyan']}4. {MODES[4]}{R}")
            print(f"  {_C['cyan']}5. {MODES[5]}{R}")
            print(f"  {_C['cyan']}6. {MODES[6]}{R}")
            print(f"  {_C['dim']}── 其他 ──{R}")
            print(f"  {_C['magenta']}7. {MODES[7]}{R}")
            print(f"  {_C['magenta']}8. {MODES[8]}{R}")
            print(f"  {_C['magenta']}9. {MODES[9]}{R}")
            raw = _input_with_timeout(
                f"\n输入模式编号（1-9），或回车退出: ",
                NEXT_MODE_TIMEOUT,
            )
            if raw is None:
                logger.info("超时，程序退出")
                break
            raw = raw.strip()
            if not raw:
                logger.info("用户选择退出")
                break
            if raw in ("1", "2", "3", "4", "5", "6", "7", "8", "9"):
                mode = int(raw)
                logger.info(f"切换到模式：{MODES[mode]}")
            else:
                logger.info("无效输入，程序退出")
                break


def _beep(sound: str = "Basso") -> None:
    """播放 macOS 系统提示音（不阻塞，失败静默）"""
    import subprocess
    try:
        subprocess.Popen(["afplay", f"/System/Library/Sounds/{sound}.aiff"])
    except Exception:
        pass


def _notify_telegram(error: Exception) -> None:
    """报错时发送 Telegram 通知，失败静默"""
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        tb = traceback.format_exception(error)
        tb_short = "".join(tb[-3:])[:1000]
        text = f"⚠ 刀剣乱舞脚本异常退出\n\n{type(error).__name__}: {error}\n\n{tb_short}"
        httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
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
        _notify_telegram(e)
        raise

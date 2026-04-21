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
from api.fatigue_recovery import run_fatigue_recovery
from api.practice import run_practice_cycle
from api.dungeon import run_dungeon_climb, run_dungeon_floor_loop
from api.sortie import (
    run_sortie_4_2_loop, run_sortie_4_3_loop, run_sortie_4_4_loop,
    run_sortie_5_2_loop, run_sortie_6_1_loop, run_sortie_7_3_loop, run_sortie_7_4_loop,
)
from api.parallel_past import run_parallel_past, run_parallel_past_loop

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
    4: "循环4-4版（主委托符，队伍3，每15次检查依赖札）",
    5: "循环4-3版（主冷却材，队伍3，每15次固定休息）",
    6: "循环7-3版（队伍3，每15次固定休息，跳过最终战）",
    13: "循环4-2版（主砥石，队伍3，每15次固定休息）",
    14: "循环5-2版（主木炭，队伍3，每15次固定休息）",
    15: "循环6-1版（主砥石，高速枪/马匹减半，推荐极短速度>150）",
    7: "习合模式（领取短刀 + 自动习合到乱舞7）",
    8: "刀解模式（领取非短刀 + 自动刀解释放刀位）",
    9: "综合管理（习合+刀解一体化，刀位满自动腾位）",
    10: "气力恢复（指定队伍全员跑1-1补满气力）",
    11: "循环7-4版（日常任务 + 7-4无限循环，队伍3，每15次固定休息）",
    12: "循环异去（日常任务 + 异去无限循环，直到次数耗尽）",
}


def get_credentials() -> tuple[str, str]:
    """获取凭证：自动登录或手动输入"""
    R = _C["reset"]
    print(f"\n{_C['bold']}获取凭证：{R}")
    print(f"  {_C['green']}1. 自动登录（Chrome + Google OAuth）{R}")
    print(f"  {_C['dim']}2. 手动输入（从 DevTools 复制）{R}")

    choice = input("选择（1/2）: ").strip()

    if choice == "1":
        from auth.auto_token import auto_get_token
        return auto_get_token()
    else:
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
    print(f"  {_C['yellow']}12. {MODES[12]}{R}")
    print(f"  {_C['dim']}── 普通地图循环 ──{R}")
    print(f"  {_C['cyan']}13. {MODES[13]}{R}")
    print(f"  {_C['cyan']}5. {MODES[5]}{R}")
    print(f"  {_C['cyan']}4. {MODES[4]}{R}")
    print(f"  {_C['cyan']}14. {MODES[14]}{R}")
    print(f"  {_C['cyan']}15. {MODES[15]}{R}")
    print(f"  {_C['cyan']}6. {MODES[6]}{R}")
    print(f"  {_C['cyan']}11. {MODES[11]}{R}")
    print(f"  {_C['dim']}── 其他 ──{R}")
    print(f"  {_C['magenta']}7. {MODES[7]}{R}")
    print(f"  {_C['magenta']}8. {MODES[8]}{R}")
    print(f"  {_C['magenta']}9. {MODES[9]}{R}")
    print(f"  {_C['magenta']}10. {MODES[10]}{R}")
    print(f"  {_C['dim']}支持接力：如 9 3:88 表示先跑模式9再跑模式3(88层){R}")
    print(f"  {_C['dim']}         10:3 表示恢复队伍3气力{R}")
    while True:
        raw = input("输入模式: ").strip()
        if not raw:
            continue
        queue = _parse_mode_queue(raw)
        if queue:
            return queue
        print("  格式错误，示例：9 3:88 或单个数字如 3")


def _register_names_from_state(state: dict) -> None:
    """从 home/index 响应的 sword 数据中提取刀名，补全名字库"""
    from api.composition import register_sword_names, get_sword_name
    swords = state.get("sword", {})
    if not swords:
        return
    # 检查是否有 name 字段（取第一把看看）
    sample = next(iter(swords.values()), {})
    name_field = None
    for field in ("name", "sword_name"):
        if field in sample:
            name_field = field
            break
    if not name_field:
        # home/index 也没有 name 字段，打一次 debug 日志
        extra = set(sample.keys()) - {
            "serial_id", "sword_id", "protect", "ranbu_level", "ranbu_exp",
            "rarity", "role_id", "hp", "hp_max", "level", "fatigue",
            "status", "equip_serial_id1", "equip_serial_id2",
            "equip_serial_id3", "equip_serial_id4",
        }
        # 只在有未知 sword_id 时打日志，避免重复刷屏
        unknown_count = sum(
            1 for s in swords.values()
            if get_sword_name(s.get("sword_id", 0)) == str(s.get("sword_id", 0))
        )
        if unknown_count > 0 and extra:
            logger.debug(f"home/index sword 无 name 字段，额外字段: {extra}（{unknown_count} 把刀缺名字）")
        return
    names = {}
    for sword in swords.values():
        sid = sword.get("sword_id")
        name = sword.get(name_field, "")
        if sid and name:
            names[sid] = name
    if names:
        register_sword_names(names)
        logger.info(f"从 home/index 补全 {len(names)} 种刀名")


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


def _parse_mode_queue(raw: str) -> list[tuple[int, dict]] | None:
    """
    解析模式队列输入。
    格式：'9 3:88' → [(9, {}), (3, {'layer_id': 88})]
           '3:88'  → [(3, {'layer_id': 88})]
           '7'     → [(7, {})]
           '1:50'  → [(1, {'start_layer': 50})]
    """
    queue = []
    for part in raw.split():
        if ":" in part:
            mode_str, param_str = part.split(":", 1)
        else:
            mode_str, param_str = part, ""

        if mode_str not in ("1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15"):
            return None

        mode = int(mode_str)
        params = {}
        if param_str:
            try:
                val = int(param_str)
                if mode == 1:
                    params["start_layer"] = val
                elif mode == 3:
                    params["layer_id"] = val
                elif mode == 10:
                    params["party_no"] = val
            except ValueError:
                return None

        queue.append((mode, params))

    return queue if queue else None


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


def _run_mode(client: ToukenClient, mode: int, state: dict, params: dict | None = None) -> None:
    """执行指定模式，params 为接力模式预设的参数"""
    global _daily_done
    params = params or {}

    def _ensure_daily():
        global _daily_done
        if not _daily_done:
            run_daily(client, state)
            _daily_done = True
        else:
            logger.info("日常任务本次 session 已执行，跳过")

    if mode == 1:
        if "start_layer" not in params:
            params["start_layer"] = int(input("请输入当前地下城层数（爬楼起始层）: ").strip())
        _ensure_daily()
        run_dungeon_climb(client, start_layer=params["start_layer"])

    elif mode == 2:
        _ensure_daily()

    elif mode == 3:
        if "layer_id" not in params:
            params["layer_id"] = int(input("请输入循环层数（如 88）: ").strip())
        _ensure_daily()
        run_dungeon_floor_loop(client, layer_id=params["layer_id"])

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

    elif mode == 10:
        if "party_no" not in params:
            params["party_no"] = int(input("请输入队伍编号（1-5）: ").strip())
        run_fatigue_recovery(client, params["party_no"])

    elif mode == 11:
        _ensure_daily()
        run_sortie_7_4_loop(client)

    elif mode == 12:
        _ensure_daily()
        run_parallel_past_loop(client)

    elif mode == 13:
        _ensure_daily()
        run_sortie_4_2_loop(client)

    elif mode == 14:
        _ensure_daily()
        run_sortie_5_2_loop(client)

    elif mode == 15:
        _ensure_daily()
        run_sortie_6_1_loop(client)


def _send_mode_summary(mode: int, error: Exception | None = None) -> None:
    """模式完成后发 Telegram 汇总"""
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        status = f"异常：{error}" if error else "正常完成"
        text = f"模式 {mode}（{MODES.get(mode, '未知')}）{status}"
        import httpx as _httpx
        _httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception:
        pass


def _show_mode_menu() -> None:
    """显示带颜色的模式列表"""
    R = _C["reset"]
    print(f"\n{_C['bold']}可选模式：{R}")
    print(f"  {_C['dim']}── 日常 ──{R}")
    print(f"  {_C['green']}2. {MODES[2]}{R}")
    print(f"  {_C['dim']}── 活动循环 ──{R}")
    print(f"  {_C['yellow']}1. {MODES[1]}{R}")
    print(f"  {_C['yellow']}3. {MODES[3]}{R}")
    print(f"  {_C['yellow']}12. {MODES[12]}{R}")
    print(f"  {_C['dim']}── 普通地图循环 ──{R}")
    print(f"  {_C['cyan']}13. {MODES[13]}{R}")
    print(f"  {_C['cyan']}5. {MODES[5]}{R}")
    print(f"  {_C['cyan']}4. {MODES[4]}{R}")
    print(f"  {_C['cyan']}14. {MODES[14]}{R}")
    print(f"  {_C['cyan']}15. {MODES[15]}{R}")
    print(f"  {_C['cyan']}6. {MODES[6]}{R}")
    print(f"  {_C['cyan']}11. {MODES[11]}{R}")
    print(f"  {_C['dim']}── 其他 ──{R}")
    print(f"  {_C['magenta']}7. {MODES[7]}{R}")
    print(f"  {_C['magenta']}8. {MODES[8]}{R}")
    print(f"  {_C['magenta']}9. {MODES[9]}{R}")
    print(f"  {_C['magenta']}10. {MODES[10]}{R}")


def main():
    global _daily_done
    sword, initial_t = get_credentials()
    queue = select_mode()  # 返回 [(mode, params), ...]

    with ToukenClient(sword=sword, uid=UID, server=SERVER) as client:
        client._csrf_token = initial_t

        state = client.get_game_state()
        if state.get("status", 0) != 0:
            raise RuntimeError(
                f"home/index 返回 status={state.get('status')}，"
                "sword / token 已过期，请重新从 DevTools 复制凭证"
            )
        handle_leave_requests(client, state)

        # 从 home/index 的 sword 数据尝试补全刀名库
        _register_names_from_state(state)

        res = state["resource"]
        logger.info(
            f"资源 — 木炭:{res['charcoal']}  玉钢:{res['steel']}  "
            f"冷却剂:{res['coolant']}  砥石:{res['file']}"
        )

        if len(queue) > 1:
            logger.info(f"接力模式：{' → '.join(MODES.get(m, str(m)) for m, _ in queue)}")

        while True:
            # 执行队列中的模式（用索引而非 for-in，方便 token 失效后原地重跑）
            i = 0
            while i < len(queue):
                mode, params = queue[i]
                logger.info(f"运行模式：{MODES.get(mode, str(mode))}")
                error = None
                retry_same_mode = False
                try:
                    _run_mode(client, mode, state, params)
                except KeyboardInterrupt:
                    logger.info("用户中断（Ctrl+C），当前模式结束")
                    # Ctrl+C 可能打断了正在进行的 API 请求，token 链断裂。
                    # 尝试 home/index 检查 session 是否还活着。
                    try:
                        state = client.get_game_state()
                        logger.info("session 正常，可继续选择模式")
                    except Exception:
                        logger.warning("Ctrl+C 后 token 已失效，如后续模式报错请重启脚本")
                except RuntimeError as e:
                    err_str = str(e)
                    if "status=91" in err_str or "status=97" in err_str:
                        is_maintenance = "status=97" in err_str
                        reason = "服务器维护（status=97）" if is_maintenance else "Token 过期（status=91）"
                        logger.info(f"{reason}，准备自动恢复...")
                        _notify_telegram(e)

                        if is_maintenance:
                            # 等到维护结束（每周二 12:00-15:00）
                            import datetime
                            now = datetime.datetime.now()
                            resume_hour = 15
                            resume_time = now.replace(hour=resume_hour, minute=5, second=0)
                            if now < resume_time:
                                wait_secs = (resume_time - now).total_seconds()
                                logger.info(f"维护中，等待到 {resume_hour}:05（{int(wait_secs//60)} 分钟后）...")
                                time.sleep(wait_secs)
                            else:
                                logger.info("已过维护时间，30秒后重试...")
                                time.sleep(30)

                        try:
                            from auth.auto_token import auto_get_token
                            new_sword, new_t = auto_get_token()
                            client._sword = new_sword  # setter 同步 cookie
                            client._csrf_token = new_t  # auto_get_token 已在 Chrome 活着时调过 startup
                            state = client.get_game_state()
                            handle_leave_requests(client, state)
                            if is_maintenance or "status=91" in err_str:
                                _daily_done = False
                            logger.info(f"自动重新登录成功，继续执行模式 {mode}（{MODES.get(mode, str(mode))}）...")
                            _send_telegram(
                                f"✓ 自动恢复成功（{reason}）\n继续执行模式 {mode}：{MODES.get(mode, str(mode))}"
                            )
                            retry_same_mode = True  # 不递增 i，下轮 while 重跑当前 mode
                        except Exception as re_err:
                            logger.error(f"自动重新登录失败：{re_err}")
                            error = re_err
                            _notify_telegram(re_err)
                    else:
                        error = e
                        logger.error(f"模式异常退出：{e}")
                        _notify_telegram(e)
                except Exception as e:
                    error = e
                    logger.error(f"模式异常退出：{e}")
                    _notify_telegram(e)

                if retry_same_mode:
                    continue  # 不 i+=1，重跑当前 mode

                # 接力模式中每个模式完成发 Telegram
                if len(queue) > 1:
                    _send_mode_summary(mode, error)

                i += 1

            # 队列执行完，等待选择下一个
            logger.info(f"全部模式完成，{NEXT_MODE_TIMEOUT // 60} 分钟内可选择下一个模式，超时自动退出")
            _show_mode_menu()
            raw = _input_with_timeout(
                f"\n输入模式（支持接力如 9 3:88），或回车退出: ",
                NEXT_MODE_TIMEOUT,
            )
            if raw is None:
                logger.info("超时，程序退出")
                break
            raw = raw.strip()
            if not raw:
                logger.info("用户选择退出")
                break
            new_queue = _parse_mode_queue(raw)
            if new_queue:
                queue = new_queue
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


def _send_telegram(text: str) -> None:
    """发送 Telegram 文本通知，失败静默"""
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception:
        pass


def _notify_telegram(error: Exception) -> None:
    """报错时发送 Telegram 通知，失败静默"""
    tb = traceback.format_exception(error)
    tb_short = "".join(tb[-3:])[:1000]
    _send_telegram(f"⚠ 刀剣乱舞脚本异常退出\n\n{type(error).__name__}: {error}\n\n{tb_short}")


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

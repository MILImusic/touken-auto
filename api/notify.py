"""
Telegram 通知模块 —— 统一发送入口

所有需要发 Telegram 的地方都调这里，不再各自写 httpx.post。
"""

import os
import traceback

import httpx
from loguru import logger

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = "YOUR_TELEGRAM_CHAT_ID"


def send_telegram(text: str) -> None:
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


def notify_error(error: Exception) -> None:
    """报错时发送 Telegram 通知（含 traceback），失败静默"""
    tb = traceback.format_exception(error)
    tb_short = "".join(tb[-3:])[:1000]
    send_telegram(f"⚠ 刀剣乱舞脚本异常退出\n\n{type(error).__name__}: {error}\n\n{tb_short}")


def notify_fatigue_stuck(serial_id: int, rounds: int) -> None:
    """气力恢复卡在0时通知"""
    send_telegram(f"⚠ 气力恢复异常\nserial_id={serial_id} 跑了 {rounds} 轮 1-1 后气力仍为 0（可能过疲劳）")


def notify_new_sword(swords: list[dict]) -> None:
    """锻造出新刀时通知"""
    from .composition import get_sword_name
    names = [f"{get_sword_name(s.get('sword_id', 0))}(id={s.get('sword_id')})" for s in swords]
    send_telegram(f"★ 限时锻造新刀！\n{chr(10).join(names)}")


def notify_resource_shortage(slot_no: int, resources: dict, recipe_amount: int) -> None:
    """锻造资源不足时通知"""
    send_telegram(
        f"槽{slot_no} 资源不足，跳过锻造\n"
        f"木炭:{resources.get('charcoal',0)} 玉钢:{resources.get('steel',0)} "
        f"冷却材:{resources.get('coolant',0)} 砥石:{resources.get('file',0)}\n"
        f"委托符:{resources.get('bill',0)}\n"
        f"需要：各{recipe_amount}"
    )

"""
自动获取 token —— opencv 模板匹配 + pyautogui 鼠标模拟 + net-log 抓 token

流程：
  1. 启动 Chrome（原始 Profile + net-log）
  2. opencv 截图匹配按钮 → pyautogui 点击
  3. 从 net-log 解析 reflect → 提取 token
  4. 关闭 Chrome
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path

import cv2
import httpx
import numpy as np
import pyautogui
from loguru import logger

CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_PROFILE_DIR = "Profile 1"
CHROME_USER_DATA = "/Users/toevskyastora/Library/Application Support/Google/Chrome"
GAME_URL = "https://play.games.dmm.com/game/tohken"
NET_LOG_PATH = "/tmp/touken-chrome-net.json"
TEMPLATES_DIR = Path(__file__).parent / "templates"

RETINA_SCALE = 2  # macOS Retina 缩放倍数


def _confirm_profile() -> bool:
    """读取 Chrome Profile 账号信息，自动确认"""
    prefs_path = Path(CHROME_USER_DATA) / CHROME_PROFILE_DIR / "Preferences"
    if not prefs_path.exists():
        return False
    try:
        prefs = json.loads(prefs_path.read_text())
        accounts = prefs.get("account_info", [])
        profile_name = prefs.get("profile", {}).get("name", "未知")
        if accounts:
            email = accounts[0].get("email", "未知")
            logger.info(f"Chrome Profile：{profile_name}（{email}）")
            return True
        return False
    except Exception:
        return False


def _launch_chrome() -> subprocess.Popen:
    """启动 Chrome"""
    subprocess.run(["pkill", "-9", "-f", "Google Chrome"], capture_output=True)
    subprocess.run(["killall", "-9", "Google Chrome"], capture_output=True)
    time.sleep(3)

    Path(CHROME_USER_DATA, "SingletonLock").unlink(missing_ok=True)
    Path(NET_LOG_PATH).unlink(missing_ok=True)

    proc = subprocess.Popen([
        CHROME_PATH,
        f"--profile-directory={CHROME_PROFILE_DIR}",
        f"--log-net-log={NET_LOG_PATH}",
        "--net-log-capture-mode=Everything",
        "--no-first-run",
        "--no-default-browser-check",
        GAME_URL,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    logger.info("Chrome 已启动")
    return proc


def _find_and_click(template_name: str, label: str, timeout: int = 20, confidence: float = 0.7) -> bool:
    """
    截图 → opencv 模板匹配 → 计算逻辑坐标 → pyautogui 点击。
    Retina 屏幕：截图是物理分辨率，坐标除以 RETINA_SCALE 得到逻辑坐标。
    """
    template_path = TEMPLATES_DIR / template_name
    if not template_path.exists():
        logger.warning(f"模板不存在：{template_path}")
        return False

    template = cv2.imread(str(template_path))
    if template is None:
        logger.warning(f"无法读取模板：{template_path}")
        return False

    th, tw = template.shape[:2]

    for attempt in range(timeout):
        # 截图（物理分辨率）
        screenshot = pyautogui.screenshot()
        screen = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)

        # 模板匹配
        result = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)

        if max_val >= confidence:
            # 物理坐标中心
            phys_x = max_loc[0] + tw // 2
            phys_y = max_loc[1] + th // 2

            # 转逻辑坐标
            logic_x = phys_x // RETINA_SCALE
            logic_y = phys_y // RETINA_SCALE

            logger.info(f"找到 {label}（confidence={max_val:.3f}，逻辑坐标=({logic_x}, {logic_y})）")
            pyautogui.moveTo(logic_x, logic_y, duration=0.3)
            time.sleep(0.2)
            pyautogui.click()
            return True

        time.sleep(1)

    logger.warning(f"未找到 {label}（{timeout}秒超时）")
    return False


def _extract_token_from_netlog() -> tuple[str, str] | None:
    """从 Chrome net-log 中提取 sword 和 fuel_csrf_token"""
    log_path = Path(NET_LOG_PATH)
    if not log_path.exists():
        return None
    try:
        content = log_path.read_text(errors="ignore")
        sword_matches = re.findall(r'sword=([a-zA-Z0-9]+)', content)
        token_matches = re.findall(r'fuel_csrf_token=([a-fA-F0-9]+)', content)
        if sword_matches:
            unique_swords = list(dict.fromkeys(sword_matches))
            logger.debug(
                f"net-log {len(content)//1024}KB — "
                f"sword: {len(sword_matches)} 匹配, {len(unique_swords)} 唯一值: "
                f"{[s[:12]+'...' for s in unique_swords]}"
            )
        if sword_matches and token_matches:
            return sword_matches[-1], token_matches[-1]
        elif sword_matches:
            # 只有 sword 没有 fuel_csrf_token
            return sword_matches[-1], ""
    except Exception as e:
        logger.debug(f"解析 net-log 失败：{e}")
    return None


def _call_startup(sword: str) -> str:
    """
    Chrome 还活着时调 startup()，用 sword 获取全新 CSRF token 链。
    杀掉 Chrome 后 sword session 会失效，必须在杀之前调用。
    """
    uid = os.getenv("UID", "14052501")
    server = os.getenv("SERVER", "w021")
    proxy = os.getenv("PROXY", "http://127.0.0.1:7897")

    resp = httpx.post(
        f"https://{server}.touken-ranbu.jp/home/startup?uid={uid}",
        data={"sword": sword},
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://www.touken-ranbu.jp",
            "Referer": "https://www.touken-ranbu.jp/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/146.0.0.0 Safari/537.36"
            ),
        },
        cookies={"sword": sword},
        proxy=proxy,
        timeout=30,
    )
    body = resp.json()
    token = body.get("t")
    if not token:
        raise RuntimeError(f"startup 失败: {resp.text[:200]}")
    logger.info("startup 成功，token chain 已建立")
    return token


def auto_get_token() -> tuple[str, str]:
    """全自动获取 token"""
    if not _confirm_profile():
        raise RuntimeError("Chrome Profile 确认失败")

    chrome_proc = None

    try:
        chrome_proc = _launch_chrome()
        time.sleep(5)

        # 1. 点击 Google 登录
        clicked = _find_and_click("google_login.png", "Google 登录", timeout=15)
        if clicked:
            time.sleep(8)
            # Google OAuth 可能在新标签页打开，切换到最新标签页
            pyautogui.hotkey("command", "shift", "]")
            logger.debug("切换到最新标签页")
            time.sleep(2)
        else:
            logger.info("未找到 Google 登录（可能已登录），继续...")

        # 2. 点击绿色箭头
        _find_and_click("green_arrow.png", "绿色箭头", timeout=30)
        time.sleep(3)

        # 3. 点击本丸
        _find_and_click("honmaru.png", "本丸", timeout=30)

        # 4. 等游戏完全加载（本丸后游戏会调 home/index 等多个 API，
        #    net-log 需要足够时间刷盘才有完整数据）
        logger.info("等待游戏完全加载（15秒）...")
        time.sleep(15)

        # 5. 提取 token，等待稳定（游戏空闲后 token 不再变化）
        logger.info("提取 token...")
        last_result: tuple[str, str] | None = None
        stable_count = 0
        for _ in range(30):
            time.sleep(2)
            result = _extract_token_from_netlog()
            if not result or not result[1]:
                continue
            if last_result and result[0] == last_result[0] and result[1] == last_result[1]:
                stable_count += 1
                if stable_count >= 2:  # 连续 2 次（4秒）相同 → 游戏已空闲
                    sword, token = result
                    logger.info(f"Token 已稳定！sword={sword[:20]}...")
                    return sword, token
            else:
                last_result = result
                stable_count = 0

        # 兜底：用最后一次的结果
        if last_result and last_result[1]:
            sword, token = last_result
            logger.warning(f"Token 未完全稳定，使用最新值：sword={sword[:20]}...")
            return sword, token

        raise RuntimeError("等待 token 超时")

    finally:
        if chrome_proc:
            chrome_proc.terminate()
            try:
                chrome_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                chrome_proc.kill()
            # 彻底杀掉残留子进程（GoogleUpdater 等）
            subprocess.run(["pkill", "-9", "-f", "Google Chrome"], capture_output=True, timeout=3)
            subprocess.run(["pkill", "-9", "-f", "GoogleUpdater"], capture_output=True, timeout=3)
            logger.info("Chrome 已关闭")
        Path(NET_LOG_PATH).unlink(missing_ok=True)


if __name__ == "__main__":
    sword, token = auto_get_token()
    print(f"\nsword: {sword}")
    print(f"fuel_csrf_token: {token}")

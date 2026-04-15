"""
自动获取 token —— pyautogui 模拟鼠标 + Chrome net-log 抓 token

流程：
  1. 启动 Chrome（原始 Profile，不需要 remote debugging）
  2. 导航到游戏页 → DMM 登录页
  3. 获取 Chrome 窗口位置，按相对坐标点击 Google 登录
  4. 等待跳转回游戏页 → 点击绿色箭头 → 点击本丸
  5. 从 Chrome net-log 文件解析 reflect 响应 → 提取 token
  6. 关闭 Chrome

依赖：pyautogui, Pillow, opencv-python-headless
"""

import json
import re
import subprocess
import time
from pathlib import Path

import pyautogui
from loguru import logger

CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_PROFILE_DIR = "Profile 1"
CHROME_USER_DATA = "/Users/toevskyastora/Library/Application Support/Google/Chrome"
GAME_URL = "https://play.games.dmm.com/game/tohken"
NET_LOG_PATH = "/tmp/touken-chrome-net.json"

# 按钮在 Chrome 窗口中的相对位置（0~1）
GOOGLE_BTN_REL = (0.49, 0.25)        # DMM 登录页 Google 按钮
GREEN_ARROW_REL = (0.50, 0.65)       # 游戏页绿色箭头（iframe 内）
HONMARU_REL = (0.50, 0.75)           # 标题画面本丸按钮


def _confirm_profile() -> bool:
    """读取 Chrome Profile 账号信息，自动确认"""
    prefs_path = Path(CHROME_USER_DATA) / CHROME_PROFILE_DIR / "Preferences"
    if not prefs_path.exists():
        logger.warning(f"找不到 Profile 配置：{prefs_path}")
        return False
    try:
        prefs = json.loads(prefs_path.read_text())
        accounts = prefs.get("account_info", [])
        profile_name = prefs.get("profile", {}).get("name", "未知")
        if accounts:
            email = accounts[0].get("email", "未知")
            name = accounts[0].get("full_name", "未知")
            logger.info(f"Chrome Profile：{profile_name}（{email} / {name}）")
            return True
        else:
            logger.warning(f"Chrome Profile：{profile_name}（无账号信息）")
            return False
    except Exception as e:
        logger.warning(f"读取 Profile 失败：{e}")
        return False


def _launch_chrome() -> subprocess.Popen:
    """启动 Chrome（原始 Profile + net-log）"""
    subprocess.run(["pkill", "-9", "-f", "Google Chrome"], capture_output=True)
    subprocess.run(["killall", "-9", "Google Chrome"], capture_output=True)
    time.sleep(3)

    lock_file = Path(CHROME_USER_DATA) / "SingletonLock"
    lock_file.unlink(missing_ok=True)
    Path(NET_LOG_PATH).unlink(missing_ok=True)

    proc = subprocess.Popen([
        CHROME_PATH,
        f"--profile-directory={CHROME_PROFILE_DIR}",
        f"--log-net-log={NET_LOG_PATH}",
        "--net-log-capture-mode=Everything",
        "--no-first-run",
        "--no-default-browser-check",
        GAME_URL,
    ])
    logger.info("Chrome 已启动")
    return proc


def _get_chrome_window() -> tuple[int, int, int, int] | None:
    """用 AppleScript 获取 Chrome 前台窗口的 (left, top, width, height)"""
    script = '''
    tell application "Google Chrome"
        set b to bounds of front window
        return (item 1 of b as string) & "," & (item 2 of b as string) & "," & (item 3 of b as string) & "," & (item 4 of b as string)
    end tell
    '''
    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
        parts = [int(x.strip()) for x in result.stdout.strip().split(",")]
        left, top, right, bottom = parts
        return left, top, right - left, bottom - top
    except Exception as e:
        logger.warning(f"获取 Chrome 窗口失败：{e}")
        return None


def _click_in_window(rel_x: float, rel_y: float, label: str) -> bool:
    """在 Chrome 窗口的相对位置点击"""
    win = _get_chrome_window()
    if not win:
        return False

    left, top, width, height = win
    x = left + int(width * rel_x)
    y = top + int(height * rel_y)

    logger.debug(f"点击 {label}：窗口({left},{top},{width}x{height}) → 坐标({x},{y})")
    pyautogui.moveTo(x, y, duration=0.3)
    time.sleep(0.2)
    pyautogui.click()
    logger.info(f"已点击 {label}（{x}, {y}）")
    return True


def _extract_token_from_netlog() -> tuple[str, str] | None:
    """从 Chrome net-log 文件中提取 sword 和 fuel_csrf_token"""
    log_path = Path(NET_LOG_PATH)
    if not log_path.exists():
        return None
    try:
        content = log_path.read_text(errors="ignore")
        sword_matches = re.findall(r'sword=([a-zA-Z0-9]+)', content)
        token_matches = re.findall(r'fuel_csrf_token=([a-fA-F0-9]+)', content)
        if sword_matches and token_matches:
            return sword_matches[-1], token_matches[-1]
    except Exception as e:
        logger.debug(f"解析 net-log 失败：{e}")
    return None


def auto_get_token() -> tuple[str, str]:
    """全自动获取 token"""
    if not _confirm_profile():
        raise RuntimeError("Chrome Profile 确认失败")

    chrome_proc = None

    try:
        # 1. 启动 Chrome
        chrome_proc = _launch_chrome()
        time.sleep(5)

        # 2. 点击 Google 登录（如果在 DMM 登录页）
        _click_in_window(*GOOGLE_BTN_REL, "Google 登录")
        time.sleep(8)

        # 3. 点击绿色箭头
        _click_in_window(*GREEN_ARROW_REL, "绿色箭头")
        time.sleep(5)

        # 4. 点击本丸
        _click_in_window(*HONMARU_REL, "本丸")

        # 5. 等待 token 出现在 net-log 中
        logger.info("等待 token...")
        for _ in range(30):
            time.sleep(1)
            result = _extract_token_from_netlog()
            if result:
                sword, token = result
                logger.info(f"Token 获取成功！sword={sword[:20]}...")
                return sword, token

        raise RuntimeError("等待 token 超时")

    finally:
        if chrome_proc:
            chrome_proc.terminate()
            try:
                chrome_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                chrome_proc.kill()
            logger.info("Chrome 已关闭")
        Path(NET_LOG_PATH).unlink(missing_ok=True)


if __name__ == "__main__":
    sword, token = auto_get_token()
    print(f"\nsword: {sword}")
    print(f"fuel_csrf_token: {token}")

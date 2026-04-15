"""
自动获取 token —— pyautogui 模拟鼠标 + Chrome net-log 抓 token

流程：
  1. 启动 Chrome（原始 Profile，不需要 remote debugging）
  2. 导航到游戏页 → DMM 登录页
  3. pyautogui 截图识别 + 模拟鼠标点击 Google 登录
  4. 等待跳转回游戏页 → 点击绿色箭头 → 点击本丸
  5. 从 Chrome net-log 文件解析 reflect 响应的 Set-Cookie → 提取 token
  6. 关闭 Chrome

依赖：pyautogui, Pillow
"""

import json
import time
import subprocess
import re
from pathlib import Path

import pyautogui
from loguru import logger

CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_PROFILE_DIR = "Profile 1"
CHROME_USER_DATA = "/Users/toevskyastora/Library/Application Support/Google/Chrome"
GAME_URL = "https://play.games.dmm.com/game/tohken"
NET_LOG_PATH = "/tmp/touken-chrome-net.json"

# 超时
TOTAL_TIMEOUT = 90  # 整个登录流程最大超时


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
        else:
            logger.info(f"Chrome Profile：{profile_name}（无账号信息）")
            return False

        return True
    except Exception as e:
        logger.warning(f"读取 Profile 失败：{e}")
        return False


def _launch_chrome() -> subprocess.Popen:
    """启动 Chrome（原始 Profile + net-log，无需 remote debugging）"""
    subprocess.run(["pkill", "-9", "-f", "Google Chrome"], capture_output=True)
    subprocess.run(["killall", "-9", "Google Chrome"], capture_output=True)
    time.sleep(3)

    # 清理锁文件和旧日志
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


def _wait_and_click_google_login() -> bool:
    """等待 DMM 登录页出现，用 pyautogui 点击 Google 登录按钮"""
    logger.info("等待 DMM 登录页...")

    for attempt in range(20):
        time.sleep(1)
        try:
            screenshot = pyautogui.screenshot()
            # 在截图中找 "Google" 文字区域（登录按钮在页面上方）
            # 使用 pyautogui.locateOnScreen 配合模板图片更可靠
            # 先用简单方案：找到后点击
            location = pyautogui.locateOnScreen(
                str(Path(__file__).parent / "templates" / "google_login.png"),
                confidence=0.7,
            )
            if location:
                center = pyautogui.center(location)
                logger.info(f"找到 Google 登录按钮：({center.x}, {center.y})")
                pyautogui.moveTo(center.x, center.y, duration=0.3)
                time.sleep(0.2)
                pyautogui.click()
                return True
        except Exception:
            pass

    return False


def _wait_and_click_arrow() -> bool:
    """等待游戏页面加载，点击绿色箭头"""
    logger.info("等待绿色箭头...")

    for attempt in range(30):
        time.sleep(1)
        try:
            location = pyautogui.locateOnScreen(
                str(Path(__file__).parent / "templates" / "green_arrow.png"),
                confidence=0.7,
            )
            if location:
                center = pyautogui.center(location)
                logger.info(f"找到绿色箭头：({center.x}, {center.y})")
                pyautogui.moveTo(center.x, center.y, duration=0.3)
                time.sleep(0.2)
                pyautogui.click()
                return True
        except Exception:
            pass

    return False


def _wait_and_click_honmaru() -> bool:
    """等待标题画面，点击本丸按钮"""
    logger.info("等待本丸按钮...")

    for attempt in range(30):
        time.sleep(1)
        try:
            location = pyautogui.locateOnScreen(
                str(Path(__file__).parent / "templates" / "honmaru.png"),
                confidence=0.7,
            )
            if location:
                center = pyautogui.center(location)
                logger.info(f"找到本丸按钮：({center.x}, {center.y})")
                pyautogui.moveTo(center.x, center.y, duration=0.3)
                time.sleep(0.2)
                pyautogui.click()
                return True
        except Exception:
            pass

    return False


def _extract_token_from_netlog() -> tuple[str, str] | None:
    """从 Chrome net-log 文件中提取 sword 和 fuel_csrf_token"""
    log_path = Path(NET_LOG_PATH)
    if not log_path.exists():
        return None

    try:
        content = log_path.read_text(errors="ignore")

        # net-log 是 JSON 格式，但可能很大且不完整（Chrome 还在写）
        # 直接用正则在原始文本中搜索 Set-Cookie
        sword = ""
        token = ""

        # 找 sword cookie
        sword_matches = re.findall(r'sword=([a-zA-Z0-9]+)', content)
        if sword_matches:
            sword = sword_matches[-1]  # 取最后一个（最新的）

        # 找 fuel_csrf_token cookie
        token_matches = re.findall(r'fuel_csrf_token=([a-fA-F0-9]+)', content)
        if token_matches:
            token = token_matches[-1]

        if sword and token:
            return sword, token

    except Exception as e:
        logger.debug(f"解析 net-log 失败：{e}")

    return None


def auto_get_token() -> tuple[str, str]:
    """
    全自动获取 token。
    返回 (sword, fuel_csrf_token)。
    """
    if not _confirm_profile():
        raise RuntimeError("Chrome Profile 确认失败")

    chrome_proc = None

    try:
        # 1. 启动 Chrome
        chrome_proc = _launch_chrome()
        time.sleep(5)

        # 2. 点击 Google 登录（如果在 DMM 登录页）
        #    如果已登录会直接跳到游戏页，这一步会超时跳过
        clicked_google = _wait_and_click_google_login()
        if clicked_google:
            logger.info("已点击 Google 登录，等待跳转...")
            time.sleep(8)
        else:
            logger.info("未找到 Google 登录按钮（可能已登录），继续...")

        # 3. 点击绿色箭头
        if not _wait_and_click_arrow():
            logger.warning("未找到绿色箭头，尝试继续...")

        time.sleep(3)

        # 4. 点击本丸
        if not _wait_and_click_honmaru():
            logger.warning("未找到本丸按钮，尝试继续...")

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

        # 清理 net-log
        Path(NET_LOG_PATH).unlink(missing_ok=True)


if __name__ == "__main__":
    sword, token = auto_get_token()
    print(f"\nsword: {sword}")
    print(f"fuel_csrf_token: {token}")

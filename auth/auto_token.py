"""
自动获取 token

优先级：
  A. Chrome 已打开游戏 → 直接从 Cookie 数据库读取（秒出，不碰 Chrome）
  B. Chrome 未打开 → 完整登录流程：
     1. 启动 Chrome（原始 Profile + net-log）
     2. opencv 截图匹配按钮 → pyautogui 点击
     3. 等游戏加载完后从 net-log 提取稳定的 token
     4. 关闭 Chrome
"""

import ctypes
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import cv2
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


def _check_game_in_chrome() -> bool:
    """检测 Chrome 是否正在运行游戏页面"""
    try:
        result = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events"\n'
             '  if not (exists process "Google Chrome") then return "no"\n'
             'end tell\n'
             'tell application "Google Chrome"\n'
             '  repeat with w in windows\n'
             '    repeat with t in tabs of w\n'
             '      if URL of t contains "touken-ranbu" or '
             'URL of t contains "dmm.com/game/tohken" then return "yes"\n'
             '    end repeat\n'
             '  end repeat\n'
             'end tell\n'
             'return "no"'],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() == "yes"
    except Exception:
        return False


def _decrypt_cookie(encrypted_value: bytes, key: bytes) -> str | None:
    """用 macOS CommonCrypto 解密 Chrome cookie（无需额外 pip 包）"""
    if not encrypted_value:
        return None
    if not encrypted_value.startswith(b"v10"):
        return encrypted_value.decode("utf-8", errors="ignore")

    encrypted_data = encrypted_value[3:]  # 跳过 v10 前缀
    iv = b" " * 16
    try:
        cc = ctypes.cdll.LoadLibrary("/usr/lib/system/libcommonCrypto.dylib")
        buf = ctypes.create_string_buffer(len(encrypted_data) + 16)
        out_len = ctypes.c_size_t(0)
        # CCCrypt(op, alg, options, key, keyLen, iv, dataIn, dataInLen, dataOut, dataOutAvail, dataOutMoved)
        rc = cc.CCCrypt(
            1, 0, 1,                          # kCCDecrypt, AES128, PKCS7Padding
            key, len(key), iv,
            encrypted_data, len(encrypted_data),
            buf, len(buf), ctypes.byref(out_len),
        )
        if rc == 0:
            return buf.raw[: out_len.value].decode("utf-8")
    except Exception as e:
        logger.debug(f"CommonCrypto 解密失败: {e}")
    return None


def _get_token_from_chrome_cookies() -> tuple[str, str] | None:
    """从 Chrome Cookie 数据库直接读取 sword + fuel_csrf_token"""
    # 1. Keychain 取 Chrome Safe Storage 密钥
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-w",
             "-s", "Chrome Safe Storage", "-a", "Chrome"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return None
        chrome_pw = r.stdout.strip().encode("utf-8")
    except Exception:
        return None

    # 2. PBKDF2 派生 AES 密钥
    derived_key = hashlib.pbkdf2_hmac("sha1", chrome_pw, b"saltysalt", 1003, dklen=16)

    # 3. 复制 Cookies 文件（Chrome 运行时锁数据库）
    for sub in ("Network/Cookies", "Cookies"):
        db_path = Path(CHROME_USER_DATA) / CHROME_PROFILE_DIR / sub
        if db_path.exists():
            break
    else:
        return None

    tmp = Path("/tmp/touken-chrome-cookies-tmp")
    try:
        shutil.copy2(db_path, tmp)
        for ext in ("-wal", "-journal"):
            src = Path(str(db_path) + ext)
            if src.exists():
                shutil.copy2(src, Path(str(tmp) + ext))
    except Exception:
        return None

    # 4. 读取并解密
    try:
        conn = sqlite3.connect(str(tmp))
        rows = conn.execute(
            "SELECT name, encrypted_value FROM cookies "
            "WHERE host_key LIKE '%touken-ranbu%' "
            "AND name IN ('sword', 'fuel_csrf_token')"
        ).fetchall()
        conn.close()
    except Exception:
        return None
    finally:
        tmp.unlink(missing_ok=True)
        for ext in ("-wal", "-journal"):
            Path(str(tmp) + ext).unlink(missing_ok=True)

    cookies = {}
    for name, enc_val in rows:
        val = _decrypt_cookie(enc_val, derived_key)
        if val:
            cookies[name] = val

    sword = cookies.get("sword")
    csrf = cookies.get("fuel_csrf_token")
    if sword and csrf:
        logger.info(f"从 Chrome Cookie 直接获取 token！sword={sword[:20]}...")
        return sword, csrf
    return None


def auto_get_token() -> tuple[str, str]:
    """全自动获取 token"""
    # 优先：Chrome 已打开游戏 → 从 Cookie 秒取
    if _check_game_in_chrome():
        logger.info("检测到 Chrome 已打开游戏，尝试从 Cookie 获取...")
        result = _get_token_from_chrome_cookies()
        if result:
            return result
        logger.warning("Cookie 获取失败，回退到完整登录流程...")

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

        # 4. 等游戏 API 调完（startup → index → reflect 约 3-5 秒）
        logger.info("等待游戏加载...")
        time.sleep(5)

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

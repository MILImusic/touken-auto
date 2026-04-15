"""
自动获取 token —— 启动 Chrome → Google 登录 DMM → 拦截 reflect 拿 token → 关 Chrome

流程：
  1. 启动 Chrome（Profile 1 + remote debugging）
  2. 导航 play.games.dmm.com/game/tohken → 跳转 DMM 登录页
  3. 点击 Google 登录按钮（a[href*="sns_type=google"]）
  4. 自动跳转回游戏页 → 点击 iframe 中央（绿色箭头）
  5. 标题画面 → 点击"本丸"按钮（iframe 下半部中央）
  6. 拦截 reflect 响应 → 提取 sword + fuel_csrf_token
  7. 关闭 Chrome

依赖：websocket-client, httpx
"""

import json
import time
import subprocess
from pathlib import Path

import httpx
import websocket
from loguru import logger

CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_PROFILE_DIR = "Profile 1"
CHROME_USER_DATA = "/Users/toevskyastora/Library/Application Support/Google/Chrome"
CDP_PORT = 9222
GAME_URL = "https://play.games.dmm.com/game/tohken"
TEMP_USER_DATA = "/tmp/touken-chrome-auto"

# 超时设置
LOGIN_PAGE_TIMEOUT = 15      # 等待 DMM 登录页加载
REDIRECT_TIMEOUT = 30        # 等待 Google OAuth 跳转回游戏页
GAME_LOAD_TIMEOUT = 30       # 等待游戏 iframe 加载
TOKEN_TIMEOUT = 30           # 等待 reflect 响应


class CDPClient:
    """简易 Chrome DevTools Protocol 客户端"""

    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=60)
        self._id = 0

    def send(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        msg = {"id": self._id, "method": method}
        if params:
            msg["params"] = params
        self.ws.send(json.dumps(msg))

        # 等待对应的响应
        while True:
            resp = json.loads(self.ws.recv())
            if resp.get("id") == self._id:
                return resp
            # 事件消息先忽略

    def send_no_wait(self, method: str, params: dict | None = None) -> None:
        self._id += 1
        msg = {"id": self._id, "method": method}
        if params:
            msg["params"] = params
        self.ws.send(json.dumps(msg))

    def recv_event(self, timeout: float = 30) -> dict | None:
        """接收一个事件，超时返回 None"""
        self.ws.settimeout(timeout)
        try:
            return json.loads(self.ws.recv())
        except websocket.WebSocketTimeoutException:
            return None

    def close(self):
        self.ws.close()


def _launch_chrome() -> subprocess.Popen:
    """启动 Chrome 带 remote debugging（不指定 user-data-dir，让 Chrome 用默认路径）"""
    subprocess.run(["pkill", "-9", "-f", "Google Chrome"], capture_output=True)
    subprocess.run(["killall", "-9", "Google Chrome"], capture_output=True)
    time.sleep(3)

    # 清理锁文件
    lock_file = Path(CHROME_USER_DATA) / "SingletonLock"
    lock_file.unlink(missing_ok=True)

    proc = subprocess.Popen([
        CHROME_PATH,
        f"--remote-debugging-port={CDP_PORT}",
        f"--profile-directory={CHROME_PROFILE_DIR}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        GAME_URL,
    ])
    logger.info("Chrome 已启动")
    return proc


def _connect_cdp() -> CDPClient:
    """连接 CDP WebSocket"""
    for attempt in range(10):
        try:
            resp = httpx.get(f"http://localhost:{CDP_PORT}/json", timeout=3)
            pages = resp.json()
            for page in pages:
                if "webSocketDebuggerUrl" in page:
                    ws_url = page["webSocketDebuggerUrl"]
                    logger.debug(f"CDP 连接：{ws_url}")
                    return CDPClient(ws_url)
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("无法连接 Chrome CDP")


def _wait_for_url(cdp: CDPClient, url_contains: str, timeout: int) -> bool:
    """等待页面 URL 包含指定字符串"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = cdp.send("Runtime.evaluate", {
                "expression": "window.location.href"
            })
            current_url = result.get("result", {}).get("result", {}).get("value", "")
            if url_contains in current_url:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _click_google_login(cdp: CDPClient) -> bool:
    """点击 DMM 页面上的 Google 登录按钮"""
    result = cdp.send("Runtime.evaluate", {
        "expression": """
            (() => {
                const link = document.querySelector('a[href*="sns_type=google"]');
                if (link) { link.click(); return true; }
                return false;
            })()
        """
    })
    clicked = result.get("result", {}).get("result", {}).get("value", False)
    if clicked:
        logger.info("点击 Google 登录按钮")
    return clicked


def _get_iframe_rect(cdp: CDPClient) -> dict | None:
    """获取 game_frame iframe 的位置和尺寸"""
    result = cdp.send("Runtime.evaluate", {
        "expression": """
            (() => {
                const iframe = document.querySelector('#game_frame');
                if (!iframe) return null;
                const rect = iframe.getBoundingClientRect();
                return JSON.stringify({
                    x: rect.x, y: rect.y,
                    width: rect.width, height: rect.height
                });
            })()
        """
    })
    value = result.get("result", {}).get("result", {}).get("value")
    if value:
        return json.loads(value)
    return None


def _click_in_iframe(cdp: CDPClient, rect: dict, rel_x: float, rel_y: float) -> None:
    """在 iframe 内的相对位置点击（rel_x/rel_y 为 0~1 的比例）"""
    x = rect["x"] + rect["width"] * rel_x
    y = rect["y"] + rect["height"] * rel_y

    for event_type in ("mousePressed", "mouseReleased"):
        cdp.send("Input.dispatchMouseEvent", {
            "type": event_type,
            "x": x,
            "y": y,
            "button": "left",
            "clickCount": 1,
        })
    logger.debug(f"点击坐标：({x:.0f}, {y:.0f})")


def _enable_network_and_wait_reflect(cdp: CDPClient) -> tuple[str, str] | None:
    """
    启用 Network 监听，等待 reflect 响应，提取 sword 和 fuel_csrf_token。
    """
    cdp.send("Network.enable")
    logger.info("等待 reflect 响应...")

    deadline = time.time() + TOKEN_TIMEOUT
    while time.time() < deadline:
        event = cdp.recv_event(timeout=2)
        if event is None:
            continue

        method = event.get("method", "")
        params = event.get("params", {})

        if method == "Network.responseReceived":
            url = params.get("response", {}).get("url", "")
            if "reflect" in url and "uid=" in url:
                # 从 response headers 拿 Set-Cookie
                headers = params.get("response", {}).get("headers", {})
                set_cookie = headers.get("set-cookie", "") or headers.get("Set-Cookie", "")

                sword = ""
                token = ""

                # 尝试从 headers 直接解析
                if set_cookie:
                    for part in set_cookie.split("\n"):
                        part = part.strip()
                        if part.startswith("sword="):
                            sword = part.split("sword=")[1].split(";")[0]
                        elif part.startswith("fuel_csrf_token="):
                            token = part.split("fuel_csrf_token=")[1].split(";")[0]

                # 如果 headers 里没有，尝试用 Network.getResponseBody 或 cookies
                if not sword or not token:
                    request_id = params.get("requestId")
                    if request_id:
                        try:
                            cookies_resp = cdp.send("Network.getCookies", {
                                "urls": [url]
                            })
                            for cookie in cookies_resp.get("result", {}).get("cookies", []):
                                if cookie["name"] == "sword":
                                    sword = cookie["value"]
                                elif cookie["name"] == "fuel_csrf_token":
                                    token = cookie["value"]
                        except Exception:
                            pass

                if sword and token:
                    logger.info(f"Token 获取成功！sword={sword[:20]}...")
                    return sword, token

    return None


def _confirm_profile() -> bool:
    """读取 Chrome Profile 账号信息，让用户确认"""
    import pathlib
    prefs_path = pathlib.Path(CHROME_USER_DATA) / CHROME_PROFILE_DIR / "Preferences"
    if not prefs_path.exists():
        logger.warning(f"找不到 Profile 配置：{prefs_path}")
        return True  # 找不到就跳过确认

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
        return True


def auto_get_token() -> tuple[str, str]:
    """
    全自动获取 token。
    返回 (sword, fuel_csrf_token)。
    """
    if not _confirm_profile():
        raise RuntimeError("用户取消登录")

    chrome_proc = None
    cdp = None

    try:
        # 1. 启动 Chrome
        chrome_proc = _launch_chrome()
        time.sleep(3)

        # 2. 连接 CDP
        cdp = _connect_cdp()

        # 3. 等待 DMM 登录页
        logger.info("等待 DMM 登录页...")
        if not _wait_for_url(cdp, "accounts.dmm.com", LOGIN_PAGE_TIMEOUT):
            # 可能已经登录过，直接在游戏页
            if _wait_for_url(cdp, "play.games.dmm.com", 5):
                logger.info("已登录，跳过登录步骤")
            else:
                raise RuntimeError("等待登录页超时")
        else:
            # 4. 点击 Google 登录
            time.sleep(2)
            if not _click_google_login(cdp):
                raise RuntimeError("找不到 Google 登录按钮")

            # 5. 等待跳转回游戏页
            logger.info("等待 Google OAuth 跳转...")
            if not _wait_for_url(cdp, "play.games.dmm.com", REDIRECT_TIMEOUT):
                raise RuntimeError("Google OAuth 跳转超时")

        # 6. 启用网络监听（提前，不漏掉 reflect）
        cdp.send("Network.enable")

        # 7. 等待 iframe 加载
        logger.info("等待游戏 iframe 加载...")
        iframe_rect = None
        deadline = time.time() + GAME_LOAD_TIMEOUT
        while time.time() < deadline:
            iframe_rect = _get_iframe_rect(cdp)
            if iframe_rect and iframe_rect["width"] > 100:
                break
            time.sleep(1)

        if not iframe_rect:
            raise RuntimeError("游戏 iframe 未加载")

        logger.info(f"iframe 加载完成：{iframe_rect['width']:.0f}x{iframe_rect['height']:.0f}")

        # 8. 点击绿色箭头（iframe 中央）
        time.sleep(2)
        logger.info("点击绿色箭头...")
        _click_in_iframe(cdp, iframe_rect, 0.5, 0.65)

        # 9. 等一下，点击"本丸"按钮（iframe 下半部中央）
        time.sleep(5)
        logger.info("点击本丸按钮...")
        _click_in_iframe(cdp, iframe_rect, 0.55, 0.75)

        # 10. 等待 reflect 响应
        result = _enable_network_and_wait_reflect(cdp)
        if not result:
            raise RuntimeError("等待 reflect 响应超时")

        return result

    finally:
        if cdp:
            try:
                cdp.close()
            except Exception:
                pass
        if chrome_proc:
            chrome_proc.terminate()
            try:
                chrome_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                chrome_proc.kill()
            logger.info("Chrome 已关闭")


if __name__ == "__main__":
    sword, token = auto_get_token()
    print(f"\nsword: {sword}")
    print(f"fuel_csrf_token: {token}")

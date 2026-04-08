"""
DMM 登录模块 —— 支持两种方式获取 sword cookie：
  1. 从运行中的 Chrome 直接读取（推荐）：用户正常登录游戏后，脚本自动提取
  2. Playwright 半自动登录（备用）：弹出浏览器等待手动完成 Google 登录

流程：
  1. 脚本打开浏览器，跳转到 DMM 登录页
  2. 你手动点「Googleでログイン」完成登录（包括 Google OAuth 跳转）
  3. 脚本检测到进入游戏首页后，自动保存 session
  4. 后续运行直接复用保存的 session，无需再次登录
"""

import json
from pathlib import Path
from playwright.sync_api import sync_playwright
from pycookiecheat import chrome_cookies

AUTH_STATE_PATH = Path(__file__).parent.parent / "auth_state.json"

# 刀剣乱舞游戏入口（DMM game player 页）
GAME_URL = "https://www.touken-ranbu.jp/"


def login_manual() -> dict:
    """
    打开浏览器，等待你手动完成 Google 登录，保存 session。
    返回 cookies dict。
    """
    print("=" * 50)
    print("请在弹出的浏览器中完成登录：")
    print("  1. 点击「Googleでログイン」")
    print("  2. 选择 Google 账号，完成授权")
    print("  3. 等待进入游戏首页，脚本自动保存 session")
    print("=" * 50)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        page.goto("https://www.dmm.com/my/-/login/")

        # 等待跳转到游戏相关页面（最多等3分钟，留时间给 Google OAuth）
        page.wait_for_url("*touken-ranbu.jp*", timeout=180_000)

        # 确保游戏首页加载完成
        page.wait_for_load_state("networkidle", timeout=30_000)

        context.storage_state(path=str(AUTH_STATE_PATH))
        cookies = {c["name"]: c["value"] for c in context.cookies()}
        browser.close()

    print(f"Session 已保存到 {AUTH_STATE_PATH}")
    return cookies


def load_cookies_from_state() -> dict:
    """从已保存的 auth_state.json 读取 cookies"""
    with open(AUTH_STATE_PATH) as f:
        state = json.load(f)
    return {c["name"]: c["value"] for c in state.get("cookies", [])}


def get_sword_from_chrome_cdp(cdp_url: str = "http://localhost:9222") -> str | None:
    """
    通过 Chrome Remote Debugging Protocol 读取 sword cookie。
    需要 Chrome 以 --remote-debugging-port=9222 启动。
    """
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp_url)
            for context in browser.contexts:
                for cookie in context.cookies():
                    if cookie["name"] == "sword" and "touken-ranbu" in cookie["domain"]:
                        return cookie["value"]
    except Exception:
        pass
    return None


def wait_for_chrome_login(poll_interval: int = 3) -> str:
    """
    等待用户在 Chrome（需开启 --remote-debugging-port=9222）里登录游戏。
    检测到 sword cookie 后自动继续。
    """
    import time
    print("=" * 50)
    print("请在 Chrome 里打开刀剣乱舞并登录进入本丸...")
    print("（Chrome 需以 --remote-debugging-port=9222 启动）")
    print("脚本检测到登录后自动继续。")
    print("=" * 50)

    while True:
        sword = get_sword_from_chrome_cdp()
        if sword:
            print("✓ 检测到 sword，开始执行任务")
            return sword
        time.sleep(poll_interval)


def is_session_valid() -> bool:
    """检查 auth_state.json 是否存在且包含 sword cookie"""
    if not AUTH_STATE_PATH.exists():
        return False
    cookies = load_cookies_from_state()
    return "sword" in cookies

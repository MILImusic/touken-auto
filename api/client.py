"""
游戏 API 基础客户端

核心机制：
  - Double Submit：sword 同时作为 Cookie 和 POST body 参数
  - CSRF Token 链式轮换：
      * JSON 型端点（startup/index 等）：新 token 在响应 JSON["t"]
      * Action 型端点（conquest/start 等）：响应无 JSON body，
        新 token 在响应 Set-Cookie: fuel_csrf_token=...
  - _post() 自动处理两种情况
"""

import time
import httpx
from loguru import logger

RETRY_COUNT: int = 5
RETRY_DELAY: int = 15  # 秒


class ToukenClient:
    BASE_URL = "https://{server}.touken-ranbu.jp"

    def __init__(self, sword: str, uid: str, server: str = "w021"):
        self.uid = uid
        self.server = server
        self.base = self.BASE_URL.format(server=server)

        # sword 同时是 cookie 和 body 参数
        self._sword = sword
        self._csrf_token: str | None = None

        self._http = httpx.Client(
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://www.touken-ranbu.jp",
                "Referer": "https://www.touken-ranbu.jp/",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
            cookies={"sword": sword},
            proxy="http://127.0.0.1:7897",
            timeout=30.0,
        )

    @property
    def _csrf_token(self) -> str | None:
        return self.__csrf_token

    @_csrf_token.setter
    def _csrf_token(self, value: str | None):
        """token 更新时同步写入 cookie jar（Double Submit Cookie 模式要求）"""
        self.__csrf_token = value
        if value is not None:
            self._http.cookies.set("fuel_csrf_token", value)

    def _post(self, path: str, extra: dict | None = None) -> dict:
        """发送 POST 请求，自动附带 sword + t，并更新 token。网络异常自动重试。"""
        if self._csrf_token is None:
            raise RuntimeError("CSRF token 未初始化，请先调用 startup()")

        data = {"sword": self._sword, "t": self._csrf_token}
        if extra:
            data.update(extra)

        url = f"{self.base}/{path}?uid={self.uid}"
        logger.debug(f"POST {path}")

        for attempt in range(1, RETRY_COUNT + 1):
            try:
                resp = self._http.post(url, data=data)
                resp.raise_for_status()
                break
            except (httpx.TimeoutException, httpx.NetworkError, httpx.ReadError, httpx.RemoteProtocolError) as e:
                if attempt < RETRY_COUNT:
                    logger.warning(
                        f"{path} 网络异常（{e.__class__.__name__}），"
                        f"{RETRY_DELAY}s 后重试（{attempt}/{RETRY_COUNT}）..."
                    )
                    time.sleep(RETRY_DELAY)
                else:
                    logger.error(f"{path} 重试 {RETRY_COUNT} 次后仍失败，放弃")
                    raise

        # 优先从 JSON body 取新 token，action 类端点则从 Set-Cookie 取
        body: dict = {}
        content_type = resp.headers.get("content-type", "")
        if "json" in content_type or (resp.content and resp.content.strip().startswith(b"{")):
            body = resp.json()

        if "t" in body:
            self._csrf_token = body["t"]
        elif "fuel_csrf_token" in resp.cookies:
            self._csrf_token = resp.cookies["fuel_csrf_token"]
            logger.debug(f"{path} token 从 Set-Cookie 更新")
        else:
            logger.warning(f"{path} 未找到新 token，保持旧值")

        status = body.get("status", 0)
        if status == 91:
            logger.error(f"{path} 返回 status=91，游戏已重置（次日更新），请重新登录")
            raise RuntimeError("游戏 status=91：session 已失效，请重新获取 sword / token")
        if status == 97:
            logger.error(f"{path} 返回 status=97，服务器维护中，停止运行")
            raise RuntimeError("游戏 status=97：服务器维护中")
        if status != 0:
            logger.warning(f"{path} 返回 status={status}")

        return body

    def startup(self) -> dict:
        """
        初始化：获取第一个 CSRF token。
        startup 请求不需要 t 参数（它是 token 链的起点）。
        """
        data = {"sword": self._sword}
        url = f"{self.base}/home/startup?uid={self.uid}"
        resp = self._http.post(url, data=data)
        resp.raise_for_status()

        body: dict = {}
        content_type = resp.headers.get("content-type", "")
        if "json" in content_type or (resp.content and resp.content.strip().startswith(b"{")):
            body = resp.json()

        if "t" in body:
            self._csrf_token = body["t"]
        elif "fuel_csrf_token" in resp.cookies:
            self._csrf_token = resp.cookies["fuel_csrf_token"]
        else:
            logger.error(f"startup 失败，响应内容：{resp.text[:200]}")
            raise RuntimeError("startup 未返回 token，sword cookie 可能已过期")

        logger.info(f"startup 成功，token 已初始化")
        return body

    def get_game_state(self) -> dict:
        """
        获取完整游戏状态（部隊、远征、资源等）。
        对应 home/index 端点。
        """
        return self._post("home/index")

    def get_conquest_data(self) -> dict:
        """
        获取远征页完整数据，包含所有刀剑的气力值（fatigue 字段）。
        对应 conquest 端点。
        """
        return self._post("conquest")

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

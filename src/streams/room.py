import re
import threading
import time
import urllib.parse
from typing import cast

import httpx

from src import JS_SCRIPT_PATH
from src.network import policy
from src.network import urls as network_urls
from src.runtime import javascript
from src.streams.ttwid import get_ttwid
from src.support import i18n, utils
from src.support.logger import logger


class UnsupportedUrlError(Exception):
    pass


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
    + "Chrome/141.0.0.0 Mobile Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
    "Cookie": "",
}

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


async def get_xbogus(url: str, headers: dict[str, str] | None = None) -> str:
    """为 url 的 query 计算 X-Bogus 签名（node 执行 src/javascript/x-bogus.js）。"""
    if not headers or "user-agent" not in (k.lower() for k in headers):
        headers = HEADERS
    query = urllib.parse.urlparse(url).query
    user_agent = next((v for k, v in headers.items() if k.lower() == "user-agent"), HEADERS["User-Agent"])
    return cast(str, await javascript.run_js_async(f"{JS_SCRIPT_PATH}/x-bogus.js", "sign", query, user_agent))


async def get_sec_user_id(
    url: str, proxy_addr: str | None = None, headers: dict[str, str] | None = None
) -> tuple[str, str] | None:
    """获取房间ID和用户secID。"""
    if not headers or all(k.lower() not in ["user-agent", "cookie"] for k in headers):
        headers = HEADERS

    try:
        proxy_addr = network_urls.normalize_proxy_url(proxy_addr)
        async with httpx.AsyncClient(proxy=proxy_addr, timeout=15, verify=policy.verify_tls) as client:
            response = await client.get(url, headers=headers, follow_redirects=True)
            redirect_url = response.url
            if "reflow/" in str(redirect_url):
                match = re.search(r"sec_user_id=([\w_\-]+)(?=&|#|$)", str(redirect_url))
                if match:
                    sec_user_id = match.group(1)
                    room_id = str(redirect_url).split("?")[0].rsplit("/", maxsplit=1)[1]
                    return room_id, sec_user_id
                raise RuntimeError("Could not find sec_user_id in the URL.")
            raise UnsupportedUrlError("The redirect URL does not contain 'reflow/'.")
    except UnsupportedUrlError as e:
        raise e
    except Exception as e:
        raise RuntimeError(f"An error occurred: {utils.mask_credentials(str(e))}") from e


def is_user_homepage_url(url: str) -> bool:
    return bool(re.search(r"douyin\.com/user/[\w\-]+", url))


def extract_sec_user_id(redirect_url: str) -> str:
    query_match = re.search(r"[?&]sec_uid=([\w\-]+)", redirect_url)
    if query_match:
        return query_match.group(1)
    path = redirect_url.split("?")[0].rstrip("/")
    path_match = re.search(r"/(?:user|share/user)/([\w\-]+)$", path)
    if path_match:
        return path_match.group(1)
    segments = [seg for seg in path.split("/") if seg]
    return segments[-1] if segments else ""


_SEC_UID_UNIQUE_CACHE: dict[str, tuple[str, float]] = {}
_SEC_UID_CACHE_LOCK = threading.Lock()
_SEC_UID_CACHE_TTL = 30 * 60


def _get_cached_unique_id(sec_user_id: str) -> str | None:
    now = time.monotonic()
    with _SEC_UID_CACHE_LOCK:
        entry = _SEC_UID_UNIQUE_CACHE.get(sec_user_id)
        if entry is not None and (now - entry[1]) < _SEC_UID_CACHE_TTL:
            return entry[0]
    return None


def _set_cached_unique_id(sec_user_id: str, unique_id: str) -> None:
    with _SEC_UID_CACHE_LOCK:
        _SEC_UID_UNIQUE_CACHE[sec_user_id] = (unique_id, time.monotonic())


async def get_unique_id(url: str, proxy_addr: str | None = None, headers: dict[str, str] | None = None) -> str | None:
    if not headers or all(k.lower() not in ["user-agent", "cookie"] for k in headers):
        headers = HEADERS
    headers = dict(headers)

    try:
        proxy_addr = network_urls.normalize_proxy_url(proxy_addr)
        async with httpx.AsyncClient(proxy=proxy_addr, timeout=15, verify=policy.verify_tls) as client:
            sec_user_id = extract_sec_user_id(url) if is_user_homepage_url(url) else ""
            if not sec_user_id:
                response = await client.get(url, headers=headers, follow_redirects=True)
                redirect_url = str(response.url)
                if "reflow/" in redirect_url:
                    raise UnsupportedUrlError("Unsupported URL")
                sec_user_id = extract_sec_user_id(redirect_url)
                if not sec_user_id:
                    raise RuntimeError(f"Could not extract sec_user_id from {redirect_url}")
            cached_unique_id = _get_cached_unique_id(sec_user_id)
            if cached_unique_id is not None:
                return cached_unique_id
            ttwid_cookie = await get_ttwid(proxy_addr)
            if ttwid_cookie:
                headers["Cookie"] = ttwid_cookie

            api_headers = {**headers, "User-Agent": DESKTOP_UA, "Referer": "https://www.douyin.com/"}
            api = f"https://www.iesdouyin.com/web/api/v2/user/info/?sec_uid={sec_user_id}"
            try:
                api_response = await client.get(api, headers=api_headers, follow_redirects=True)
                user_info = cast(dict[str, object], cast(dict[str, object], api_response.json()).get("user_info") or {})
                unique_id_value = user_info.get("unique_id")
                if isinstance(unique_id_value, str) and unique_id_value:
                    _set_cached_unique_id(sec_user_id, unique_id_value)
                    return unique_id_value
                short_id_value = user_info.get("short_id")
                if isinstance(short_id_value, str) and short_id_value and short_id_value != "0":
                    _set_cached_unique_id(sec_user_id, short_id_value)
                    return short_id_value
            except Exception as e:
                logger.debug(
                    i18n.tr(
                        "抖音 unique_id 接口失败（转 HTML 兜底）: {type_name}: {e} - {masked_url}",
                        type_name=type(e).__name__,
                        e=e,
                        masked_url=utils.mask_credentials(api),
                    )
                )

            user_page_response = await client.get(
                f"https://www.iesdouyin.com/share/user/{sec_user_id}", headers=headers, follow_redirects=True
            )
            matches = re.findall(r'unique_id":"(.*?)","verification_type', user_page_response.text)
            if matches:
                unique_id = cast(str, matches[-1])
                _set_cached_unique_id(sec_user_id, unique_id)
                return unique_id
            raise RuntimeError(f"Could not resolve unique_id for sec_user_id={sec_user_id}")
    except UnsupportedUrlError as e:
        raise e
    except Exception as e:
        raise RuntimeError(f"An error occurred: {utils.mask_credentials(str(e))}") from e


if __name__ == "__main__":
    import asyncio

    room_url = "https://v.douyin.com/iQLgKSj/"
    result = asyncio.run(get_sec_user_id(room_url))
    if result is not None:
        _room_id, sec_uid = result
        print("return sec_user_id:", sec_uid)

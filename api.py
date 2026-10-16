"""亚服 HTTP 适配器：共享节流、有限重试和有界缓存。"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from io import BytesIO
import math
import random
import time
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from .cache import AsyncCache, BusyError
from .config import API_BASE_URL, Settings
from .models import Account, Ship, ShipType, StatsResult, StatsStatus

IMAGE_HOSTS = frozenset({"wows-gloss-icons.wgcdn.co"})
SHIP_FIELDS = (
    "ship_id,ship_id_str,name,tier,nation,type,is_premium,is_special,"
    "images.contour,images.large"
)


@dataclass(frozen=True)
class ShipPage:
    page_no: int
    page_total: int
    total: int
    ships: tuple[Ship, ...]


class ApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        business_code: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ):
        self.http_status = http_status
        self.business_code = business_code
        self.retryable = retryable
        self.retry_after = retry_after
        super().__init__(message)


def integer(value: object, *, minimum: int = 0) -> int:
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    if type(value) is not int or value < minimum:
        raise ApiError("API 响应格式错误")
    return value


def parse_ship(data: object, key: str | None = None) -> Ship:
    if not isinstance(data, dict):
        raise ApiError("舰船数据格式错误")
    try:
        ship_id = integer(data["ship_id"], minimum=1)
        if key is not None and ship_id != integer(key, minimum=1):
            raise ApiError("舰船 ID 与目录键不一致")
        tier = integer(data["tier"], minimum=1)
        name, nation = data["name"], data["nation"]
        if tier > 11 or not isinstance(name, str) or not name.strip():
            raise ApiError("舰船等级或名称无效")
        if not isinstance(nation, str) or not nation:
            raise ApiError("舰船国家无效")
        if type(data["is_premium"]) is not bool or type(data["is_special"]) is not bool:
            raise ApiError("舰船属性格式错误")
        images = data.get("images") or {}
        url = images.get("contour")
        if url is not None and not isinstance(url, str):
            raise ApiError("舰图地址格式错误")
        image_url = images.get("large")
        if image_url is not None and not isinstance(image_url, str):
            raise ApiError("高清舰图地址格式错误")
        code = data.get("ship_id_str")
        if code is not None and (
            not isinstance(code, str) or not code.isascii() or not code
        ):
            raise ApiError("舰船客户端代码格式错误")
        return Ship(
            ship_id,
            name,
            tier,
            nation,
            ShipType(data["type"]),
            data["is_premium"],
            data["is_special"],
            url,
            image_url,
            code,
        )
    except (KeyError, ValueError, TypeError, AttributeError) as error:
        raise ApiError("舰船数据格式错误") from error


class WowsApi(Protocol):
    async def fetch_ship_page(self, page_no: int, language: str) -> ShipPage: ...
    async def search_accounts(self, nickname: str) -> tuple[Account, ...]: ...
    async def fetch_stats(
        self, account: Account, *, force: bool = False
    ) -> StatsResult: ...
    async def fetch_image(self, url: str) -> bytes: ...
    async def close(self) -> None: ...


def retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (
                parsedate_to_datetime(value) - datetime.now(timezone.utc)
            ).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0, seconds) if math.isfinite(seconds) else None


def validate_image_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname in IMAGE_HOSTS
            and parsed.port in {None, 443}
            and not parsed.username
            and not parsed.password
        )
    except (ValueError, TypeError) as error:
        raise ApiError("舰图地址不是已验证的官方 CDN") from error
    if not valid:
        raise ApiError("舰图地址不是已验证的官方 CDN")


class CircuitOpen(ApiError):
    def __init__(self, retry_at):
        self.retry_at = retry_at
        super().__init__("WG 接口正在退避，请稍后再试。")


class HttpWowsApi:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        if not settings.configured:
            raise ValueError("选船 API 未启用或未配置 APP_ID")
        self.settings = settings
        self.client = client or httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )
        self.owns_client = client is None
        self._schedule_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(4)
        self._last_request = -math.inf
        self._pending_count = 0
        self._closed = False
        self.circuit = {
            "failures": 0,
            "retry_at": 0,
            "reason": None,
            "last_success_at": 0,
        }
        self._circuit_probe = False
        self.stats_cache = AsyncCache[tuple[str, int], StatsResult](
            settings.stats_ttl_minutes * 60, 1000
        )
        self.search_cache = AsyncCache[str, tuple[Account, ...]](60, 200)
        self.image_cache = AsyncCache[str, bytes](7 * 86400, 64, 16)

    async def _schedule(self) -> None:
        async with self._schedule_lock:
            remaining = self.settings.min_request_interval_ms / 1000 - (
                time.monotonic() - self._last_request
            )
            if remaining > 0:
                await asyncio.sleep(remaining)
            self._last_request = time.monotonic()

    async def _attempt(self, operation):
        if self._closed:
            raise ApiError("API 客户端已关闭")
        if self._pending_count >= 64:
            raise BusyError("API 请求队列已满")
        self._pending_count += 1
        try:
            async with self._slots:
                for attempt in range(self.settings.max_retries + 1):
                    await self._schedule()
                    try:
                        return await operation()
                    except (httpx.TimeoutException, httpx.TransportError) as error:
                        failure = ApiError("网络暂不可用", retryable=True)
                        failure.__cause__ = error
                    except ApiError as error:
                        failure = error
                    if not failure.retryable or attempt == self.settings.max_retries:
                        raise failure
                    delay = failure.retry_after
                    if delay is None:
                        delay = 0.5 * 2**attempt + random.uniform(0, 0.2)
                    await asyncio.sleep(delay)
        finally:
            self._pending_count -= 1

    @staticmethod
    def _check_http(response: httpx.Response) -> None:
        status = response.status_code
        if status == 429 or 500 <= status < 600:
            raise ApiError(
                "API 暂时繁忙",
                http_status=status,
                retryable=True,
                retry_after=retry_after(response.headers.get("retry-after")),
            )
        if status != 200:
            raise ApiError("HTTP 请求失败", http_status=status)

    def _circuit_failure(self, error):
        permanent = str(error) in {
            "INVALID_IP_ADDRESS",
            "INVALID_APPLICATION_ID",
            "APPLICATION_IS_BLOCKED",
        }
        temporary = error.retryable or str(error) in {"API 操作超时", "网络暂不可用"}
        if not permanent and not temporary:
            return
        failures = self.circuit["failures"] + 1
        self.circuit.update(
            failures=failures, reason=str(error) if permanent else "temporary"
        )
        if permanent or failures >= self.settings.health_failure_threshold:
            delay = (
                self.settings.api_config_retry_minutes * 60
                if permanent
                else min(
                    self.settings.api_circuit_max_hours * 3600,
                    self.settings.api_circuit_base_seconds
                    * 2 ** min(failures - self.settings.health_failure_threshold, 16),
                )
            )
            self.circuit["retry_at"] = (
                time.time() + delay + random.uniform(0, min(5, delay / 10))
            )

    async def _json(self, method: str, **params) -> dict:
        if self.circuit["retry_at"] > time.time():
            raise CircuitOpen(self.circuit["retry_at"])
        probe = self.circuit["retry_at"] > 0
        if probe and self._circuit_probe:
            raise CircuitOpen(time.time() + self.settings.api_circuit_base_seconds)
        if probe:
            self._circuit_probe = True

        async def request():
            if self.circuit["retry_at"] > time.time():
                raise CircuitOpen(self.circuit["retry_at"])

            # POST 避免应用 ID 出现在 URL/常规 HTTP 日志里。
            response = await self.client.post(
                API_BASE_URL + method + "/",
                data={"application_id": self.settings.app_id, **params},
                timeout=self.settings.request_timeout_seconds,
            )
            self._check_http(response)
            try:
                body = response.json()
                if not isinstance(body, dict):
                    raise ValueError
                if body.get("status") == "error":
                    error = body["error"]
                    code, message = integer(error["code"]), str(error["message"])
                    raise ApiError(
                        message,
                        http_status=response.status_code,
                        business_code=code,
                        retryable=message
                        in {"REQUEST_LIMIT_EXCEEDED", "SOURCE_NOT_AVAILABLE"},
                    )
                if body.get("status") != "ok" or "data" not in body:
                    raise ValueError
                return body
            except (ValueError, KeyError, TypeError) as error:
                raise ApiError("API 响应格式错误") from error

        try:
            try:
                body = await asyncio.wait_for(
                    self._attempt(request), self.settings.operation_timeout_seconds
                )
            except asyncio.TimeoutError as error:
                raise ApiError("API 操作超时", retryable=True) from error
            self.circuit.update(
                failures=0, retry_at=0, reason=None, last_success_at=time.time()
            )
            return body
        except CircuitOpen:
            raise
        except ApiError as error:
            self._circuit_failure(error)
            raise
        finally:
            if probe:
                self._circuit_probe = False

    async def fetch_ship_page(self, page_no: int, language: str) -> ShipPage:
        if language != self.settings.language:
            raise ApiError("目录语言与客户端配置不一致")
        body = await self._json(
            "encyclopedia/ships",
            page_no=page_no,
            limit=100,
            language=language,
            fields=SHIP_FIELDS,
        )
        try:
            meta, data = body["meta"], body["data"]
            page = integer(meta["page"], minimum=1)
            pages = integer(meta["page_total"], minimum=1)
            total = integer(meta["total"], minimum=1)
            if not isinstance(data, dict) or page != page_no or page > pages:
                raise ValueError
            if integer(meta["count"]) != len(data) or not data or len(data) > 100:
                raise ValueError
            return ShipPage(
                page,
                pages,
                total,
                tuple(parse_ship(value, key) for key, value in data.items()),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ApiError("目录分页格式错误") from error

    async def search_accounts(self, nickname: str) -> tuple[Account, ...]:
        nickname = nickname.strip()
        if not 3 <= len(nickname) <= 24 or not nickname.isascii():
            raise ApiError("请输入 3–24 位亚服游戏昵称")

        async def fetch():
            body = await self._json(
                "account/list", search=nickname, type="exact", limit=10
            )
            if not body["data"]:
                body = await self._json("account/list", search=nickname, limit=10)
            if not isinstance(body["data"], list):
                raise ApiError("账号列表格式错误")
            accounts = []
            for row in body["data"]:
                try:
                    account_id = integer(row["account_id"], minimum=1)
                    name = row["nickname"]
                    if not isinstance(name, str) or not name:
                        raise ValueError
                    accounts.append(Account("asia", account_id, name))
                except (KeyError, TypeError, ValueError) as error:
                    raise ApiError("账号列表格式错误") from error
            return tuple(accounts[:10])

        return await self.search_cache.get(nickname.casefold(), fetch)

    async def fetch_stats(
        self, account: Account, *, force: bool = False
    ) -> StatsResult:
        if account.server != "asia":
            raise ApiError("第一版仅支持亚服账号查询")

        async def fetch():
            body = await self._json(
                "ships/stats", account_id=account.account_id, fields="ship_id,battles"
            )
            try:
                meta = body.get("meta", {})
                if not isinstance(meta, dict):
                    raise ValueError
                hidden = meta.get("hidden")
                if hidden is None:
                    hidden = []
                if not isinstance(hidden, list):
                    raise ValueError
                if account.account_id in {
                    integer(value, minimum=1) for value in hidden
                }:
                    return StatsResult("asia", account.account_id, StatsStatus.HIDDEN)
                rows = body["data"][str(account.account_id)]
                if rows is None:
                    rows = []
                if not isinstance(rows, list):
                    raise ValueError
                ids = frozenset(
                    integer(row["ship_id"], minimum=1)
                    for row in rows
                    if integer(row["battles"]) > 0
                )
                status = StatsStatus.AVAILABLE if ids else StatsStatus.EMPTY
                return StatsResult("asia", account.account_id, status, ids)
            except (KeyError, TypeError, ValueError) as error:
                raise ApiError("个人战绩响应格式错误") from error

        key = account.server, account.account_id
        if force:
            self.stats_cache.invalidate(key)
        return await self.stats_cache.get(key, fetch)

    async def fetch_image(self, url: str) -> bytes:
        validate_image_url(url)

        async def download():
            async def request():
                async with self.client.stream(
                    "GET", url, timeout=self.settings.request_timeout_seconds
                ) as response:
                    self._check_http(response)
                    if response.headers.get("content-type", "").split(";")[0] not in {
                        "image/png",
                        "image/jpeg",
                        "image/webp",
                    }:
                        raise ApiError("舰图响应类型无效")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 4 * 1024 * 1024:
                            raise ApiError("舰图过大")
                    return bytes(data)

            try:
                raw = await asyncio.wait_for(
                    self._attempt(request), self.settings.request_timeout_seconds
                )
                return await asyncio.to_thread(self._normalize_image, raw)
            except asyncio.TimeoutError as error:
                raise ApiError("舰图下载超时") from error

        return await self.image_cache.get(url, download)

    @staticmethod
    def _normalize_image(raw: bytes) -> bytes:
        from PIL import Image

        try:
            with Image.open(BytesIO(raw)) as image:
                if (
                    image.width > 4096
                    or image.height > 4096
                    or image.format not in {"PNG", "JPEG", "WEBP"}
                ):
                    raise ApiError("舰图尺寸或格式无效")
                image.verify()
            with Image.open(BytesIO(raw)) as image:
                output = BytesIO()
                image.convert("RGBA").save(output, format="PNG")
                return output.getvalue()
        except (OSError, ValueError, Image.DecompressionBombError) as error:
            raise ApiError("舰图内容无效") from error

    async def close(self) -> None:
        self._closed = True
        await asyncio.gather(
            self.stats_cache.close(),
            self.search_cache.close(),
            self.image_cache.close(),
        )
        if self.owns_client:
            await self.client.aclose()

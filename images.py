"""校验本地 PNG 后优先读取；官方 URL 变化自然使用新缓存文件。"""

import asyncio
import hashlib
from io import BytesIO
import logging
from pathlib import Path
import shutil
import time

from .api import ApiError, WowsApi, validate_image_url
from .cache import AsyncCache
from .paths import WowsPaths
from .storage import StorageError, atomic_write_bytes

logger = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 4 * 1024 * 1024


def validate_png(raw: bytes) -> None:
    from PIL import Image

    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ApiError("本地舰图大小无效")
    try:
        with Image.open(BytesIO(raw)) as image:
            if image.format != "PNG" or image.width > 4096 or image.height > 4096:
                raise ApiError("本地舰图格式或尺寸无效")
            image.verify()
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise ApiError("本地舰图内容无效") from error


class ImageStore:
    def __init__(self, api: WowsApi, data_path: Path):
        self.api = api
        self.failures = {}
        self.retry_seconds = getattr(
            getattr(api, "settings", None), "image_retry_minutes", 10
        )
        if type(self.retry_seconds) not in {int, float}:
            self.retry_seconds = 10
        self.retry_seconds *= 60
        self.root = WowsPaths(data_path).images
        self.cache = AsyncCache[str, bytes](7 * 86400, 64, 16)

    def path_for(self, url: str) -> Path:
        validate_image_url(url)
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.png"

    @staticmethod
    def read_cached(path: Path) -> bytes | None:
        """Read a previously cached image after validating its PNG payload."""
        return ImageStore._read(path)

    @staticmethod
    def _read(path: Path) -> bytes | None:
        try:
            if path.stat().st_size > MAX_IMAGE_BYTES:
                raise ApiError("本地舰图过大")
            raw = path.read_bytes()
            validate_png(raw)
            return raw
        except FileNotFoundError:
            return None
        except (OSError, ApiError):
            logger.warning("本地舰图无效或不可读，尝试重新下载")
            return None

    async def get(self, url: str) -> bytes:
        path = self.path_for(url)

        async def fetch():
            local = await asyncio.to_thread(self._read, path)
            if local is not None:
                return local
            if time.monotonic() < self.failures.get(url, 0):
                raise ApiError("该舰图暂不可用，稍后自动重试")
            try:
                raw = await self.api.fetch_image(url)
            except Exception:
                now = time.monotonic()
                self.failures = {
                    key: until for key, until in self.failures.items() if until > now
                }
                self.failures[url] = now + self.retry_seconds
                raise
            self.failures.pop(url, None)
            await asyncio.to_thread(validate_png, raw)
            try:
                ancestor = self.root
                while not ancestor.exists():
                    ancestor = ancestor.parent
                free = (await asyncio.to_thread(shutil.disk_usage, ancestor)).free
                minimum = getattr(
                    getattr(self.api, "settings", None), "disk_min_free_mb", 512
                )
                if type(minimum) not in {int, float}:
                    minimum = 512
                if free < minimum * 1024 * 1024 + len(raw):
                    raise StorageError("剩余空间不足，舰图本次仅供内存使用")
                await atomic_write_bytes(path, raw)
            except StorageError:
                logger.warning("舰图保存失败，本次使用已下载图片")
            return raw

        return await self.cache.get(url, fetch)

    async def close(self) -> None:
        await self.cache.close()

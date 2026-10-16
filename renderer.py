"""共享 htmlrender 浏览器，仅创建/关闭本插件页面；失败保持原选船文本。"""

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from .card import card_context
from .config import Settings
from .messages import selection_text
from .models import Selection

logger = logging.getLogger(__name__)
IMAGE_PREFIXES = (
    "data:image/png;base64,",
    "data:image/jpeg;base64,",
    "data:image/webp;base64,",
)
CONTENT_SCRIPT = """async () => {
    await document.fonts.ready;
    await Promise.all(Array.from(document.images).map(img => img.decode()));
}"""


@dataclass(frozen=True)
class RenderedCard:
    text: str
    png: bytes | None = None


class ShipRenderer:
    def __init__(self, settings: Settings, browser_factory=None):
        self.settings = settings
        self.browser_factory = browser_factory
        self.slots = asyncio.Semaphore(settings.max_render_concurrency)
        self.admitted = 0
        self.tasks: set[asyncio.Task] = set()
        self.closed = False
        env = Environment(
            loader=FileSystemLoader(Path(__file__).parent / "templates"),
            autoescape=select_autoescape(["html"]),
            undefined=StrictUndefined,
        )
        self.template = env.get_template("ship_card.html")

    async def render(
        self,
        selection: Selection,
        image_data_uri: str | None = None,
        *,
        requester: str = "",
        ship_name: str | None = None,
        ship_name_en: str | None = None,
    ) -> RenderedCard:
        text = selection_text(selection)
        if (
            self.closed
            or self.admitted
            >= self.settings.max_render_concurrency + self.settings.max_render_queue
        ):
            return RenderedCard(text)
        self.admitted += 1
        task = asyncio.current_task()
        if task is not None:
            self.tasks.add(task)
        try:
            png = await asyncio.wait_for(
                self._render(
                    selection,
                    image_data_uri,
                    requester,
                    ship_name,
                    ship_name_en,
                ),
                self.settings.render_timeout_seconds,
            )
            return RenderedCard(text, png)
        except Exception as error:
            logger.warning("舰船卡片降级为文本（%s）", type(error).__name__)
            return RenderedCard(text)
        finally:
            self.admitted -= 1
            if task is not None:
                self.tasks.discard(task)

    async def _render(
        self,
        selection,
        image_data_uri,
        requester="",
        ship_name=None,
        ship_name_en=None,
    ):
        async with self.slots:
            if self.browser_factory is None:
                from nonebot_plugin_htmlrender import get_browser

                factory = get_browser
            else:
                factory = self.browser_factory
            browser = await factory()
            context = await browser.new_context(
                viewport={"width": 840, "height": 1050}, device_scale_factor=2
            )
            try:
                page = await context.new_page()

                async def block_remote(route):
                    await route.abort()

                await page.route("**/*", block_remote)
                if image_data_uri and not image_data_uri.startswith(IMAGE_PREFIXES):
                    image_data_uri = None
                html = self.template.render(
                    **card_context(
                        selection,
                        ship_name=ship_name,
                        ship_name_en=ship_name_en,
                        image_data_uri=image_data_uri,
                        requester=requester,
                    )
                )
                await page.set_content(html, wait_until="load")
                await page.evaluate(CONTENT_SCRIPT)
                return await page.locator(".card").screenshot(
                    type="png", omit_background=True
                )
            finally:
                try:
                    await asyncio.wait_for(context.close(), 2)
                except Exception:
                    logger.warning("卡片页面清理失败")

    async def close(self) -> None:
        self.closed = True
        tasks = tuple(task for task in self.tasks if task is not asyncio.current_task())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

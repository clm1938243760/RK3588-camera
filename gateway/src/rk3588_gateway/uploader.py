from __future__ import annotations

import asyncio
import logging

from aiohttp import ClientSession, ClientTimeout

from .config import UploaderConfig
from .queue import EventQueue

LOGGER = logging.getLogger(__name__)


class Uploader:
    def __init__(self, config: UploaderConfig, queue: EventQueue) -> None:
        self.config = config
        self.queue = queue
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        if not self.config.enabled:
            LOGGER.info("uploader disabled")
            return
        if not self.config.endpoint:
            LOGGER.warning("uploader endpoint is empty")
            return

        timeout = ClientTimeout(total=self.config.timeout_seconds)
        async with ClientSession(timeout=timeout) as session:
            while not self._stop.is_set():
                await self.flush_once(session)
                await asyncio.sleep(self.config.retry_interval_seconds)

    async def flush_once(self, session: ClientSession) -> None:
        batch = self.queue.get_batch(self.config.max_batch_size)
        if not batch:
            return

        ids = [event["id"] for event in batch]
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        try:
            async with session.post(self.config.endpoint, json={"events": batch}, headers=headers) as resp:
                body = await resp.text()
                if 200 <= resp.status < 300:
                    self.queue.mark_sent(ids)
                    LOGGER.info("uploaded %d event(s)", len(ids))
                else:
                    error = f"http {resp.status}: {body[:300]}"
                    self.queue.mark_failed(ids, error)
                    LOGGER.warning("upload failed: %s", error)
        except Exception as exc:
            self.queue.mark_failed(ids, str(exc))
            LOGGER.warning("upload exception: %s", exc)

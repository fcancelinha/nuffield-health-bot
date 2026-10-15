from __future__ import annotations

import httpx
import structlog

logger = structlog.get_logger()

GRAPH_API_VERSION = "v21.0"


class WhatsAppNotifier:
    """Sends messages through the WhatsApp Cloud API (Meta Graph)."""

    def __init__(
        self,
        token: str,
        phone_number_id: str,
        recipient: str,
        client: httpx.AsyncClient,
    ) -> None:
        self._client = client
        self._recipient = recipient
        self._url = (
            f"https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_number_id}/messages"
        )
        self._headers = {"Authorization": f"Bearer {token}"}

    async def notify(self, message: str) -> None:
        payload = {
            "messaging_product": "whatsapp",
            "to": self._recipient,
            "type": "text",
            "text": {"body": message},
        }
        try:
            response = await self._client.post(
                self._url, json=payload, headers=self._headers, timeout=10.0
            )
            response.raise_for_status()
        except httpx.HTTPError:
            logger.exception("whatsapp.notify_failed", text=message)


class LogNotifier:
    """Fallback when no external notification channel is configured."""

    async def notify(self, message: str) -> None:
        logger.info("notification", text=message)
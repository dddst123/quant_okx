from __future__ import annotations

import logging
from datetime import UTC, datetime

import requests

from okx_quant.config import Settings


class AlertManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.getLogger("okx_quant.alerts")

    def _payload(self, message: str) -> dict:
        fmt = self.settings.alert_format.lower()
        if fmt == "feishu":
            return {"msg_type": "text", "content": {"text": message}}
        if fmt == "discord":
            return {"content": message}
        return {"text": message}

    def send(self, title: str, message: str) -> None:
        body = f"[{datetime.now(UTC).isoformat()}] {title}\n{message}"
        if not self.settings.alert_webhook_url:
            self.logger.warning("Alert: %s | %s", title, message)
            return
        try:
            requests.post(
                self.settings.alert_webhook_url,
                json=self._payload(body),
                timeout=self.settings.alert_timeout_sec,
            ).raise_for_status()
        except Exception:
            self.logger.exception("Failed to send alert")

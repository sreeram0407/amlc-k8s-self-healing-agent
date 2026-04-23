"""Slack alert integration — replaces the pretty-print stub from the demo.

Same class/signature as OpenClawIntegration so agent.py doesn't change.
Reads SLACK_BOT_TOKEN from env; falls back to stdout if token is missing
so local dev / unit tests still work.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
except ImportError:
    WebClient = None  # type: ignore
    SlackApiError = Exception  # type: ignore

from .config import OpenClawConfig


_SEV_ICON = {"critical": "🚨", "warning": "⚠️", "info": "ℹ️"}


class SlackIntegration:
    """Sends agent escalations to Slack. Drop-in for OpenClawIntegration."""

    def __init__(self, config: OpenClawConfig) -> None:
        self.config = config
        self.alerts: list[dict[str, Any]] = []
        token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
        if token and WebClient is not None:
            self._client = WebClient(token=token)
        else:
            self._client = None
            if not token:
                print("   ⚙️  SLACK_BOT_TOKEN not set — alerts will print to stdout")
            elif WebClient is None:
                print("   ⚙️  slack_sdk not installed — alerts will print to stdout")

    def format_alert_for_tool(self, inp: dict[str, Any]) -> dict[str, Any]:
        """Handle an alert_human tool call. Posts to Slack, returns the alert dict."""
        alert = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": inp.get("severity", "info"),
            "summary": inp.get("summary", ""),
            "details": inp.get("details", ""),
            "recommended_action": inp.get("recommended_action", ""),
            "channel": self.config.channel,
        }
        self.alerts.append(alert)
        self._post(alert)
        return alert

    def _post(self, alert: dict[str, Any]) -> None:
        if self._client is None:
            self._print(alert)
            return

        icon = _SEV_ICON.get(alert["severity"], "•")
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{icon} {alert['severity'].upper()}: {alert['summary']}"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Time*\n{alert['timestamp']}"},
                    {"type": "mrkdwn", "text": f"*Severity*\n{alert['severity']}"},
                ],
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Details*\n```{alert['details'][:2000]}```"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn",
                         "text": f"*Recommended Action*\n{alert['recommended_action']}"},
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn",
                              "text": "Posted by k8s-self-healer agent"}],
            },
        ]
        try:
            self._client.chat_postMessage(
                channel=self.config.channel,
                blocks=blocks,
                text=f"{alert['severity'].upper()}: {alert['summary']}",  # fallback for notifications
            )
            print(f"   📣 Slack alert posted to {self.config.channel}")
        except SlackApiError as e:
            err = getattr(e, "response", {}).get("error", str(e)) if hasattr(e, "response") else str(e)
            print(f"   ❌ Slack post failed: {err}")
            self._print(alert)

    def _print(self, alert: dict[str, Any]) -> None:
        """Fallback when Slack is unavailable — same pretty-print as the demo."""
        icon = _SEV_ICON.get(alert["severity"], "•")
        bar = "─" * 58
        print(f"\n   ┌{bar}┐")
        print(f"   │ {icon}  ALERT → {alert['channel']:<43s}│")
        print(f"   ├{bar}┤")
        print(f"   │ severity : {alert['severity']:<45s}│")
        print(f"   │ summary  : {_trunc(alert['summary'], 45):<45s}│")
        print(f"   │ details  : {_trunc(alert['details'], 45):<45s}│")
        print(f"   │ action   : {_trunc(alert['recommended_action'], 45):<45s}│")
        print(f"   └{bar}┘")


def _trunc(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"

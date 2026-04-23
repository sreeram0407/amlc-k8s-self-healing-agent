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
    WebClient = None # type: ignore
    SlackApiError = Exception # type: ignore

from .config import OpenClawConfig


_SEV_ICON = {"critical": "[CRIT]", "warning": "[WARN]", "info": "[INFO]"}


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
                print(" SLACK_BOT_TOKEN not set — alerts will print to stdout")
            elif WebClient is None:
                print(" slack_sdk not installed — alerts will print to stdout")

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
        what_failed, why_blocked = _split_details(alert["details"])
        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {"type": "plain_text",
                         "text": f"{icon} {alert['severity'].upper()}: {alert['summary']}"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn",
                         "text": f"*What failed*\n{what_failed[:1500]}"},
            },
        ]
        if why_blocked:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn",
                         "text": f"*Why auto-fix did not apply*\n{why_blocked[:1000]}"},
            })
        blocks.extend([
            {
                "type": "section",
                "text": {"type": "mrkdwn",
                         "text": f"*What you should do*\n{alert['recommended_action']}"},
            },
            {
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": f"k8s-self-healer · severity `{alert['severity']}` · {alert['timestamp']}",
                }],
            },
        ])
        try:
            self._client.chat_postMessage(
                channel=self.config.channel,
                blocks=blocks,
                text=f"{alert['severity'].upper()}: {alert['summary']}", # fallback for notifications
            )
            print(f" Slack alert posted to {self.config.channel}")
        except SlackApiError as e:
            err = getattr(e, "response", {}).get("error", str(e)) if hasattr(e, "response") else str(e)
            print(f" [fail] Slack post failed: {err}")
            self._print(alert)

    def _print(self, alert: dict[str, Any]) -> None:
        """Fallback when Slack is unavailable — same pretty-print as the demo."""
        icon = _SEV_ICON.get(alert["severity"], "•")
        bar = "─" * 58
        print(f"\n ┌{bar}┐")
        print(f" │ {icon} ALERT -> {alert['channel']:<43s}│")
        print(f" ├{bar}┤")
        print(f" │ severity : {alert['severity']:<45s}│")
        print(f" │ summary : {_trunc(alert['summary'], 45):<45s}│")
        print(f" │ details : {_trunc(alert['details'], 45):<45s}│")
        print(f" │ action : {_trunc(alert['recommended_action'], 45):<45s}│")
        print(f" └{bar}┘")


def _trunc(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


# Markers the runbook tells the model to include in the details string to
# distinguish "what failed" from "why we didn't auto-fix".
_WHY_MARKERS = (
    "why auto-fix",
    "auto-fix didn't",
    "auto-fix did not",
    "guardrail blocked",
    "guardrail_blocked",
    "blocked by",
    "cannot auto-remediate",
    "no playbook",
)


def _split_details(details: str) -> tuple[str, str]:
    """Split the agent-provided details field into (what_failed, why_blocked).

    The SKILL.md asks the model to include a "Why auto-fix didn't apply" line in
    the details. If we find such a marker, put that paragraph (and anything
    following it) into the second return value; the rest goes to the first.
    If no marker is present, the entire text is "what failed" and we return ""
    for "why blocked" so the Slack block is omitted.
    """
    if not details:
        return "", ""
    lines = details.splitlines()
    lowered = [line.lower() for line in lines]
    split_idx = -1
    for i, line in enumerate(lowered):
        if any(m in line for m in _WHY_MARKERS):
            split_idx = i
            break
    if split_idx == -1:
        return details.strip(), ""
    return (
        "\n".join(lines[:split_idx]).strip(),
        "\n".join(lines[split_idx:]).strip(),
    )

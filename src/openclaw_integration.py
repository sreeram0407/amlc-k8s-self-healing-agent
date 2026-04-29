"""OpenClaw human-in-the-loop escalation.

Formats agent alerts for a human operator and (optionally) posts them to a
webhook. In this demo we just pretty-print to stdout.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .config import OpenClawConfig


_SEV_ICON = {"critical": "[CRIT]", "warning": "[WARN]", "info": "[INFO]"}


class OpenClawIntegration:
    def __init__(self, config: OpenClawConfig) -> None:
        self.config = config
        self.alerts: list[dict[str, Any]] = []

    def format_alert_for_tool(self, inp: dict[str, Any]) -> dict[str, Any]:
        """Handle an alert_human tool call. Returns the formatted alert."""
        alert = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": inp.get("severity", "info"),
            "summary": inp.get("summary", ""),
            "details": inp.get("details", ""),
            "recommended_action": inp.get("recommended_action", ""),
            "channel": self.config.channel,
        }
        self.alerts.append(alert)
        self._emit(alert)
        return alert

    def _emit(self, alert: dict[str, Any]) -> None:
        icon = _SEV_ICON.get(alert["severity"], "•")
        bar = "─" * 58
        print(f"\n   ┌{bar}┐")
        print(f"   │ {icon}  OpenClaw alert -> {self.config.channel:<30s}│")
        print(f"   ├{bar}┤")
        print(f"   │ severity : {alert['severity']:<45s}│")
        print(f"   │ summary  : {_trunc(alert['summary'], 45):<45s}│")
        print(f"   │ details  : {_trunc(alert['details'], 45):<45s}│")
        print(f"   │ action   : {_trunc(alert['recommended_action'], 45):<45s}│")
        print(f"   └{bar}┘")

    def post_resolution(self, pod_name: str, namespace: str,
                        action_taken: str, action_params: dict,
                        diagnosis: str = "") -> dict[str, Any]:
        """Pretty-print resolution for the local demo. Matches SlackIntegration's signature."""
        params_str = ", ".join(f"{k}={v}" for k, v in (action_params or {}).items()
                               if k not in ("pod_name", "namespace"))
        bar = "─" * 58
        print(f"\n   ┌{bar}┐")
        print(f"   │ [FIXED]  Auto-remediated -> {self.config.channel:<27s}│")
        print(f"   ├{bar}┤")
        print(f"   │ pod      : {pod_name:<45s}│")
        print(f"   │ ns       : {namespace:<45s}│")
        print(f"   │ action   : {action_taken:<45s}│")
        print(f"   │ params   : {_trunc(params_str, 45):<45s}│")
        print(f"   └{bar}┘")
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pod_name": pod_name,
            "namespace": namespace,
            "action_taken": action_taken,
            "action_params": action_params,
        }


def _trunc(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"

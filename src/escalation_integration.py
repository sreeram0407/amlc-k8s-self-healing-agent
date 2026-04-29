"""Fan out human escalations to every configured destination."""

from __future__ import annotations

from typing import Any, Protocol


class AlertDestination(Protocol):
    def format_alert_for_tool(self, inp: dict[str, Any]) -> dict[str, Any]:
        ...


class EscalationIntegration:
    """Calls multiple alert destinations without letting one block another."""

    def __init__(self, destinations: list[AlertDestination]) -> None:
        self.destinations = destinations
        self.alerts: list[dict[str, Any]] = []

    def format_alert_for_tool(self, inp: dict[str, Any]) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for destination in self.destinations:
            name = destination.__class__.__name__
            try:
                result = destination.format_alert_for_tool(inp)
                results.append({"destination": name, "ok": True, "result": result})
            except Exception as exc:  # noqa: BLE001 - alerting must not crash healing
                print(f" [fail] {name} escalation failed: {exc}")
                results.append({"destination": name, "ok": False, "error": str(exc)})

        alert = {"results": results}
        self.alerts.append(alert)
        return alert

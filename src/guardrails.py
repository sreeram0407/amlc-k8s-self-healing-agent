"""Guardrails — safety checks that gate every automated action."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import GuardrailsConfig


def _parse_memory(mem_str: str) -> int:
    """Parse K8s memory string (e.g. '512Mi') into MiB."""
    m = re.match(r"(\d+)(Mi|Gi|Ki)?", mem_str)
    if not m:
        return 0
    val = int(m.group(1))
    unit = m.group(2) or "Mi"
    if unit == "Gi":
        return val * 1024
    if unit == "Ki":
        return max(1, val // 1024)
    return val


class Guardrails:
    """Enforces safety limits on automated actions."""

    def __init__(self, config: GuardrailsConfig, cluster: Any = None) -> None:
        self.config = config
        self.cluster = cluster
        # Track action history: key → list of timestamps
        self._action_history: dict[str, list[datetime]] = defaultdict(list)
        # Track original memory limits per pod
        self._original_memory: dict[str, str] = {}
        # Global action counter
        self._global_actions: list[datetime] = []

    def _prune(self, timestamps: list[datetime], window: timedelta) -> list[datetime]:
        """Remove entries older than the window."""
        cutoff = datetime.now(timezone.utc) - window
        return [t for t in timestamps if t > cutoff]

    def check(self, action: str, params: dict[str, Any]) -> tuple[bool, str]:
        """
        Check whether an action is allowed.

        Returns (allowed, reason).
        """
        now = datetime.now(timezone.utc)
        one_hour = timedelta(hours=1)

        # --- global rate limit ---
        self._global_actions = self._prune(self._global_actions, one_hour)
        if len(self._global_actions) >= self.config.max_actions_per_hour:
            return False, f"Global rate limit reached ({self.config.max_actions_per_hour} actions/hour)"

        pod_name = params.get("pod_name", "")
        namespace = params.get("namespace", "default")

        # --- blast radius check ---
        if self.cluster is not None:
            health = self.cluster.get_namespace_health(namespace)
            if health["health_ratio"] < (1 - self.config.blast_radius_threshold):
                return (
                    False,
                    f"Blast radius threshold exceeded: {health['unhealthy_pods']}/{health['total_pods']} "
                    f"pods unhealthy in '{namespace}' (>{self.config.blast_radius_threshold * 100:.0f}% threshold). "
                    f"Escalate to human.",
                )

        # --- per-pod cooldown ---
        if pod_name:
            cooldown_key = f"cooldown:{pod_name}"
            history = self._action_history.get(cooldown_key, [])
            if history:
                last_action = max(history)
                elapsed = (now - last_action).total_seconds()
                if elapsed < self.config.cooldown_seconds:
                    remaining = int(self.config.cooldown_seconds - elapsed)
                    return False, f"Cooldown active for pod '{pod_name}': {remaining}s remaining"

        # --- action-specific checks ---
        if action == "restart_pod":
            return self._check_restart(pod_name, now, one_hour)
        elif action == "scale_deployment":
            return self._check_scale(params)
        elif action == "rollback_deployment":
            return self._check_rollback(params)
        elif action == "update_resource_limits":
            return self._check_resource_update(params)

        # Default: allow
        return True, "OK"

    def _check_restart(
        self, pod_name: str, now: datetime, window: timedelta
    ) -> tuple[bool, str]:
        key = f"restart:{pod_name}"
        self._action_history[key] = self._prune(self._action_history[key], window)
        if len(self._action_history[key]) >= self.config.max_restarts_per_hour:
            return (
                False,
                f"Pod '{pod_name}' has been restarted {len(self._action_history[key])} times in the past hour "
                f"(limit: {self.config.max_restarts_per_hour})",
            )
        return True, "OK"

    def _check_scale(self, params: dict[str, Any]) -> tuple[bool, str]:
        replicas = params.get("replicas", 0)
        if replicas > self.config.max_replicas:
            return False, f"Cannot scale beyond {self.config.max_replicas} replicas (requested {replicas})"
        if replicas < 0:
            return False, "Replica count cannot be negative"
        return True, "OK"

    def _check_rollback(self, params: dict[str, Any]) -> tuple[bool, str]:
        deployment_name = params.get("deployment_name", "")
        if self.cluster is not None:
            dep = self.cluster.get_deployment(deployment_name)
            if dep and dep.get("revision_history"):
                last_deploy_ts = dep["revision_history"][-1].get("timestamp", "")
                if last_deploy_ts:
                    try:
                        last_dt = datetime.fromisoformat(last_deploy_ts)
                        if last_dt.tzinfo is None:
                            last_dt = last_dt.replace(tzinfo=timezone.utc)
                        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
                        if elapsed > self.config.rollback_window_minutes:
                            return (
                                False,
                                f"Last deployment was {elapsed:.0f} min ago (rollback window: "
                                f"{self.config.rollback_window_minutes} min)",
                            )
                    except (ValueError, TypeError):
                        pass
        return True, "OK"

    def _check_resource_update(self, params: dict[str, Any]) -> tuple[bool, str]:
        pod_name = params.get("pod_name", "")
        new_memory = params.get("memory")

        if new_memory and pod_name:
            # Track original memory
            if pod_name not in self._original_memory:
                if self.cluster is not None:
                    pod = self.cluster.get_pod(pod_name)
                    if not pod:
                        # Try to find across all namespaces
                        for p in self.cluster.pods.values():
                            if p.name == pod_name:
                                pod = p.to_dict()
                                break
                    if pod:
                        self._original_memory[pod_name] = pod.get("memory_limit", "256Mi")
                else:
                    self._original_memory[pod_name] = "256Mi"

            original = _parse_memory(self._original_memory.get(pod_name, "256Mi"))
            requested = _parse_memory(new_memory)
            if original > 0 and requested > original * self.config.max_memory_multiplier:
                return (
                    False,
                    f"Memory increase for '{pod_name}' blocked: {new_memory} exceeds "
                    f"{self.config.max_memory_multiplier}x original ({self._original_memory.get(pod_name, '256Mi')})",
                )

        return True, "OK"

    def record_action(self, action: str, params: dict[str, Any]) -> None:
        """Record that an action was taken, for rate-limiting."""
        now = datetime.now(timezone.utc)
        pod_name = params.get("pod_name", "")
        self._global_actions.append(now)

        if pod_name:
            self._action_history[f"cooldown:{pod_name}"].append(now)

        if action == "restart_pod" and pod_name:
            self._action_history[f"restart:{pod_name}"].append(now)

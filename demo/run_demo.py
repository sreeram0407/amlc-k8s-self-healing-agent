#!/usr/bin/env python3
"""Interactive demo of the K8s Self-Healing Agent.

Run:  python -m demo.run_demo          (from project root)
  or: python demo/run_demo.py
"""

from __future__ import annotations

import json
import os
import sys
import time

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.audit import AuditLogger
from src.config import load_config
from src.guardrails import Guardrails
from src.mock_cluster import MockCluster
from src.agent import Agent
from src.openclaw_integration import OpenClawIntegration
from demo.scenarios import (
    scenario_simple_recovery,
    scenario_rollback_bad_deploy,
    scenario_guardrail_escalation,
    scenario_systemic_failure,
)


BANNER = r"""
╔═══════════════════════════════════════════════════════════╗
║        K8s Self-Healing Agent — Interactive Demo          ║
╚═══════════════════════════════════════════════════════════╝
"""

DIVIDER = "─" * 60


def _print_cluster_status(cluster: MockCluster) -> None:
    print(f"\n{DIVIDER}")
    print("📊 Cluster Status")
    print(DIVIDER)
    for ns in ("production", "staging"):
        health = cluster.get_namespace_health(ns)
        icon = "✅" if health["unhealthy_pods"] == 0 else "⚠️"
        print(
            f"  {icon} {ns}: {health['healthy_pods']}/{health['total_pods']} pods healthy"
        )
    print()


def _print_audit(entry: dict) -> None:
    print(f"\n{DIVIDER}")
    print("📋 Audit Log Entry")
    print(DIVIDER)
    for key in ("event_id", "pod_name", "namespace", "event_type", "action_taken",
                "guardrail_check", "outcome", "tokens_used"):
        val = entry.get(key, "")
        if val:
            print(f"  {key:20s}: {val}")
    diag = entry.get("diagnosis", "")
    if diag:
        # Truncate long diagnosis
        lines = diag.strip().split("\n")
        preview = "\n    ".join(lines[:6])
        if len(lines) > 6:
            preview += "\n    …"
        print(f"  {'diagnosis':20s}: {preview}")
    print()


def run_scenario(
    name_fn,
    cluster: MockCluster,
    agent: Agent,
    pause: float = 2.0,
) -> None:
    scenario = name_fn(cluster)
    if "error" in scenario:
        print(f"  ⚠️  Skipping — {scenario['error']}")
        return

    print(f"\n{'═' * 60}")
    print(f"▶ {scenario['name']}")
    print(f"{'═' * 60}")
    print(f"\n{scenario['description']}\n")

    _print_cluster_status(cluster)
    time.sleep(pause)

    entry = agent.handle_event(scenario["event"])
    _print_audit(entry)

    _print_cluster_status(cluster)
    time.sleep(pause)


def main() -> None:
    print(BANNER)

    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config.yaml",
    )
    config = load_config(config_path)
    cluster = MockCluster()
    audit = AuditLogger(db_path=":memory:")
    guardrails = Guardrails(config.guardrails, cluster=cluster)
    openclaw = OpenClawIntegration(config.openclaw)
    agent = Agent(config, cluster, audit, guardrails, openclaw)

    _print_cluster_status(cluster)
    pause = float(os.environ.get("DEMO_PAUSE", "1"))

    scenarios = [
        scenario_simple_recovery,
        scenario_rollback_bad_deploy,
        scenario_guardrail_escalation,
        scenario_systemic_failure,
    ]

    for fn in scenarios:
        try:
            run_scenario(fn, cluster, agent, pause=pause)
        except Exception as exc:
            print(f"\n  ❌ Scenario failed: {exc}\n")

    # Final audit dump
    print(f"\n{'═' * 60}")
    print("📜 Full Audit Log")
    print(f"{'═' * 60}")
    for entry in audit.get_recent(limit=20):
        ts = entry.get("timestamp", "")[:19]
        action = entry.get("action_taken", "")
        pod = entry.get("pod_name", "")
        outcome = entry.get("outcome", "")
        print(f"  {ts}  {pod:30s}  {action:25s}  {outcome}")

    print(f"\n✅ Demo complete.\n")


if __name__ == "__main__":
    main()

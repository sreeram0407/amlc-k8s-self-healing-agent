#!/usr/bin/env python3
"""Production entrypoint — poll for unhealthy pods and run the agent on each.

Invoked by a Kubernetes CronJob every 5 min. Each invocation:
  1. Connects to the cluster (in-cluster config via ServiceAccount)
  2. Lists unhealthy pods across watched namespaces
  3. For each, builds an event and calls agent.handle_event(...)
  4. Writes audit log entries to the persistent SQLite DB
  5. Exits

No long-running loop — the CronJob schedule is the loop.

Env vars:
  ANTHROPIC_API_KEY     required (or DEMO_MOCK=1 for offline testing)
  SLACK_BOT_TOKEN       required for real alerts
  WATCH_NAMESPACES      comma-separated list (default: all non-system)
  AUDIT_DB_PATH         SQLite path (default: /data/audit.db)
  CONFIG_PATH           YAML config path (default: /etc/healer/config.yaml)
  MAX_EVENTS_PER_RUN    cap on how many pods to handle per invocation (default: 5)
"""

from __future__ import annotations

import os
import sys
import time
import traceback

# Make `src/...` imports work when run as a script
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.agent import Agent
from src.audit import AuditLogger
from src.config import load_config
from src.guardrails import Guardrails
from src.k8s_cluster import KubernetesCluster
from src.slack_integration import SlackIntegration


def _event_from_pod(pod: dict) -> dict:
    """Build the event dict shape the agent expects from a real pod dict."""
    status = pod.get("status", "Unknown")
    return {
        "pod_name": pod["name"],
        "namespace": pod["namespace"],
        "reason": status,
        "status": status,
        "message": f"Pod {pod['name']} in namespace {pod['namespace']} "
                   f"detected in state {status} by self-healing poller",
    }


def main() -> int:
    print(f"\n{'═' * 60}")
    print(f"🔁 Self-healing poller run @ {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print(f"{'═' * 60}\n")

    # --- Load config ---
    config_path = os.environ.get("CONFIG_PATH", "/etc/healer/config.yaml")
    cfg = load_config(config_path)
    print(f"   • Model: {cfg.agent.model}")
    print(f"   • Alert channel: {cfg.openclaw.channel}")

    # --- Determine watched namespaces ---
    watch_raw = os.environ.get("WATCH_NAMESPACES", "").strip()
    watch_namespaces = [n.strip() for n in watch_raw.split(",") if n.strip()] or None
    if watch_namespaces:
        print(f"   • Watching namespaces: {watch_namespaces}")
    else:
        print(f"   • Watching all non-system namespaces")

    # --- Wire up the real cluster + integrations ---
    try:
        cluster = KubernetesCluster(watch_namespaces=watch_namespaces)
    except Exception as e:
        print(f"   ❌ Failed to connect to cluster: {e}")
        traceback.print_exc()
        return 1

    audit_path = os.environ.get("AUDIT_DB_PATH", "/data/audit.db")
    audit = AuditLogger(db_path=audit_path)
    guardrails = Guardrails(cfg.guardrails, cluster=cluster)
    slack = SlackIntegration(cfg.openclaw)
    agent = Agent(cfg, cluster, audit, guardrails, slack)

    # --- Find unhealthy pods ---
    try:
        unhealthy = cluster.get_unhealthy_pods()
    except Exception as e:
        print(f"   ❌ Failed to list unhealthy pods: {e}")
        return 1

    if not unhealthy:
        print("   ✅ No unhealthy pods detected — nothing to do.\n")
        return 0

    max_events = int(os.environ.get("MAX_EVENTS_PER_RUN", "5"))
    print(f"   ⚠️  Found {len(unhealthy)} unhealthy pod(s); "
          f"handling up to {max_events} this run\n")

    # --- Process each one ---
    handled = 0
    for pod in unhealthy[:max_events]:
        try:
            event = _event_from_pod(pod)
            agent.handle_event(event)
            handled += 1
        except Exception as e:
            print(f"   ❌ Agent error on {pod.get('name')}: {e}")
            traceback.print_exc()

    print(f"\n{'═' * 60}")
    print(f"✅ Poller done — handled {handled}/{len(unhealthy[:max_events])} events")
    print(f"{'═' * 60}\n")

    # --- Recent audit summary ---
    recent = audit.get_recent(limit=handled)
    for entry in recent:
        ts = entry.get("timestamp", "")[:19]
        pod_name = entry.get("pod_name", "")
        action = entry.get("action_taken", "")
        outcome = entry.get("outcome", "")
        print(f"   {ts}  {pod_name:30s}  {action:25s}  {outcome}")

    audit.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""One-shot helper to restore healer-config to its known-good original state.

Use this if the load-test harness left the ConfigMap in a broken state.
Reconstructs the original config.yaml content (verified from the cluster
before any patches) and patches it back in via `kubectl patch`.

Usage:
  python scripts/restore_healer_config.py
"""
import json
import subprocess
import sys

ORIGINAL_CONFIG_YAML = """\
agent:
  model: claude-haiku-4-5-20251001
  incident_model: claude-sonnet-4-6
  max_tokens: 2048

guardrails:
  max_restarts_per_hour: 3
  max_replicas: 10
  rollback_window_minutes: 60
  max_memory_multiplier: 2.0
  cooldown_seconds: 60
  blast_radius_threshold: 0.5
  max_actions_per_hour: 10

openclaw:
  channel: "#k8s-alerts"
  enabled: false
  webhook_url: "http://openclaw.openclaw.svc.cluster.local:18789/hooks/agent"
  agent_id: "default"
  hooks_token_env: "OPENCLAW_HOOKS_TOKEN"
  deliver: false
  paging_enabled: false
  timeout_seconds: 10
"""


def main() -> int:
    patch = {"data": {"config.yaml": ORIGINAL_CONFIG_YAML}}
    res = subprocess.run(
        ["kubectl", "patch", "configmap", "healer-config",
         "-n", "k8s-healer", "--type=merge",
         "-p", json.dumps(patch)],
        capture_output=True, text=True,
    )
    sys.stdout.write(res.stdout)
    sys.stderr.write(res.stderr)
    if res.returncode != 0:
        return res.returncode
    # Verify
    verify = subprocess.run(
        ["kubectl", "get", "cm", "healer-config", "-n", "k8s-healer",
         "-o", "jsonpath={.data.config\\.yaml}"],
        capture_output=True, text=True, check=True,
    )
    print("\n--- restored config.yaml content ---")
    print(verify.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Load-test the deployed self-healing agent on real GKE.

Picks random "easy" failure scenarios from a configurable pool, injects each
into the cluster (unique name per iteration), triggers the healer-poller
CronJob manually, waits for completion, and at the end queries the audit DB
once to aggregate per-incident metrics: success rate, decision time (wall-
clock proxy), tokens, cost.

Why one final DB query (not per-iteration): the audit PVC is RWO. Keeping a
reader pod attached at the same time as poller Jobs is contentious. Snapshot
max(id) once at the start and once at the end; match new rows to iterations
by pod name.

Usage:
  python scripts/load_test.py --runs 50 --yes
  python scripts/load_test.py --runs 5 --scenarios crashloop,oom --seed 42 --yes

Requires: kubectl authenticated to the cluster running healer-poller.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import signal
import statistics
import subprocess
import sys
import textwrap
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEALER_NS = "k8s-healer"
TARGET_NS = "default"
CRONJOB = "healer-poller"
CONFIGMAP = "healer-config"
PVC = "healer-audit"
SA = "healer-sa"
READER_DEPLOY = "audit-reader"
LOADTEST_LABEL = "demo=loadtest"


# ─── ANSI helpers ──────────────────────────────────────────────────────────

def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if sys.stdout.isatty() else s

def info(msg: str) -> None: print(_c("36", "[..]"), msg)
def ok(msg: str)   -> None: print(_c("32", "[ok]"), msg)
def warn(msg: str) -> None: print(_c("33", "[!!]"), msg)
def fail(msg: str) -> None: print(_c("31", "[xx]"), msg)


# ─── kubectl wrapper ───────────────────────────────────────────────────────

def kubectl(*args: str, check: bool = True, timeout: int = 60,
            stdin: str | None = None) -> str:
    res = subprocess.run(
        ["kubectl", *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and res.returncode != 0:
        raise RuntimeError(
            f"kubectl {' '.join(args)} failed (rc={res.returncode}):\n"
            f"  stderr: {res.stderr.strip()}\n"
            f"  stdout: {res.stdout.strip()}"
        )
    return res.stdout


# ─── Scenario definitions ──────────────────────────────────────────────────

@dataclass
class Scenario:
    sid: str
    description: str
    expected_failure_pattern: str          # regex over pod status reasons
    expected_actions: tuple[str, ...]       # action_taken values that count as "correct"
    inject: callable                        # fn(name) -> None  (kubectl apply etc.)
    cleanup: callable                       # fn(name) -> None
    pod_label_template: str                 # e.g. "app={name}"
    # For cycling failures (CrashLoopBackOff, OOMKilled), the pod alternates
    # between Running and Waiting states. We need to wait until the back-off
    # is long enough that the poller's snapshot reliably catches the Waiting
    # state. requires_restarts=2 means wait until restartCount >= 2 (back-off
    # is 20-40s by then).
    requires_restarts: int = 0


def _apply_obj(obj: dict) -> None:
    kubectl("apply", "-f", "-", stdin=json.dumps(obj))


def _apply_yaml(yaml_str: str) -> None:
    kubectl("apply", "-f", "-", stdin=yaml_str)


def _del_kind(kind: str, name: str) -> None:
    kubectl("delete", kind, name, "-n", TARGET_NS,
            "--ignore-not-found=true", "--wait=false",
            check=False, timeout=30)


def _deploy_obj(name: str, scenario_label: str, container: dict,
                run_id: str) -> dict:
    labels = {
        "app": name,
        "demo": "loadtest",
        "scenario": scenario_label,
        "loadtest-run": run_id,
    }
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": TARGET_NS, "labels": labels},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "terminationGracePeriodSeconds": 5,
                    "containers": [container],
                },
            },
        },
    }


def inject_crashloop(name: str, run_id: str) -> None:
    container = {
        "name": "app",
        "image": "busybox:1.36",
        "imagePullPolicy": "IfNotPresent",
        "command": ["/bin/sh", "-c"],
        "args": ['echo "FATAL: required env var DB_URL is not set"; exit 1'],
        "resources": {
            "requests": {"memory": "16Mi", "cpu": "10m"},
            "limits":   {"memory": "32Mi", "cpu": "100m"},
        },
    }
    _apply_obj(_deploy_obj(name, "crashloopbackoff", container, run_id))


def inject_oom(name: str, run_id: str) -> None:
    container = {
        "name": "stress",
        "image": "polinux/stress:latest",
        "imagePullPolicy": "IfNotPresent",
        "command": ["stress"],
        "args": ["--vm", "1", "--vm-bytes", "250M", "--vm-hang", "1"],
        "resources": {
            "requests": {"memory": "8Mi", "cpu": "50m"},
            "limits":   {"memory": "10Mi", "cpu": "100m"},
        },
    }
    _apply_obj(_deploy_obj(name, "oomkilled", container, run_id))


def inject_imagepull(name: str, run_id: str) -> None:
    container = {
        "name": "app",
        "image": "nginx:nonexistent-tag-12345",
        "imagePullPolicy": "Always",
        "resources": {
            "requests": {"memory": "16Mi", "cpu": "10m"},
            "limits":   {"memory": "64Mi", "cpu": "100m"},
        },
    }
    _apply_obj(_deploy_obj(name, "imagepullbackoff", container, run_id))


def inject_imagepull_rollback(name: str, run_id: str) -> None:
    """Deploy with a GOOD image first (revision 1, the rollback target),
    then push a BAD image (revision 2). The agent should roll back."""
    container_good = {
        "name": "app",
        "image": "nginx:1.27-alpine",
        "imagePullPolicy": "IfNotPresent",
        "resources": {
            "requests": {"memory": "16Mi", "cpu": "10m"},
            "limits":   {"memory": "64Mi", "cpu": "100m"},
        },
    }
    _apply_obj(_deploy_obj(name, "imagepullrollback", container_good, run_id))
    # Wait for revision 1 to roll out before pushing the bad image, otherwise
    # the rollback target won't exist.
    kubectl("rollout", "status", f"deployment/{name}", "-n", TARGET_NS,
            "--timeout=120s")
    kubectl("set", "image", f"deployment/{name}",
            "app=nginx:nonexistent-tag-12345", "-n", TARGET_NS)


def inject_pending(name: str, run_id: str) -> None:
    labels = {
        "app": name,
        "demo": "loadtest",
        "scenario": "pendingcapacity",
        "loadtest-run": run_id,
    }
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": TARGET_NS, "labels": labels},
        "spec": {
            "terminationGracePeriodSeconds": 5,
            "containers": [{
                "name": "hog",
                "image": "registry.k8s.io/pause:3.10",
                "resources": {
                    "requests": {"cpu": "100", "memory": "200Gi"},
                    "limits":   {"cpu": "100", "memory": "200Gi"},
                },
            }],
        },
    }
    _apply_obj(pod)


def cleanup_deploy(name: str) -> None:
    _del_kind("deployment", name)


def cleanup_pod(name: str) -> None:
    _del_kind("pod", name)


SCENARIOS: dict[str, Scenario] = {
    "crashloop": Scenario(
        sid="crashloop",
        description="S01: env var missing → CrashLoopBackOff",
        expected_failure_pattern=r"CrashLoopBackOff|Error",
        expected_actions=("restart_pod", "escalated"),
        inject=inject_crashloop,
        cleanup=cleanup_deploy,
        pod_label_template="app={name}",
        requires_restarts=2,
    ),
    "oom": Scenario(
        sid="oom",
        description="S01: OOMKilled (alloc 250M vs 10Mi limit)",
        expected_failure_pattern=r"OOMKilled|CrashLoopBackOff",
        expected_actions=("update_resource_limits",),
        inject=inject_oom,
        cleanup=cleanup_deploy,
        pod_label_template="app={name}",
        requires_restarts=2,
    ),
    "imagepull_noprior": Scenario(
        sid="imagepull_noprior",
        description="S03: bad image tag, no prior revision → escalate",
        expected_failure_pattern=r"ImagePullBackOff|ErrImagePull",
        expected_actions=("escalated",),
        inject=inject_imagepull,
        cleanup=cleanup_deploy,
        pod_label_template="app={name}",
    ),
    "imagepull_rollback": Scenario(
        sid="imagepull_rollback",
        description="S03: deploy good then bad → rollback",
        expected_failure_pattern=r"ImagePullBackOff|ErrImagePull",
        expected_actions=("rollback_deployment",),
        inject=inject_imagepull_rollback,
        cleanup=cleanup_deploy,
        pod_label_template="app={name}",
    ),
    "pending_capacity": Scenario(
        sid="pending_capacity",
        description="S09/S02: 100 CPU + 200Gi RAM request → Pending → escalate",
        expected_failure_pattern=r"Pending",
        expected_actions=("escalated",),
        inject=inject_pending,
        cleanup=cleanup_pod,
        pod_label_template="app={name}",
    ),
}


# ─── Pod state polling (ported from demo/e2e_test.sh:64-92) ────────────────

def wait_for_pod_state(label: str, pattern: str, timeout: int = 180) -> bool:
    deadline = time.time() + timeout
    pat = re.compile(pattern, re.IGNORECASE)
    while time.time() < deadline:
        try:
            out = kubectl("get", "pods", "-n", TARGET_NS, "-l", label,
                          "-o", "json", check=False, timeout=15)
            data = json.loads(out) if out.strip() else {"items": []}
        except Exception:
            time.sleep(3)
            continue
        for p in data.get("items", []):
            reasons = [p.get("status", {}).get("phase", "")]
            for cs in p.get("status", {}).get("containerStatuses") or []:
                w = (cs.get("state") or {}).get("waiting") or {}
                if w.get("reason"): reasons.append(w["reason"])
                t = (cs.get("state") or {}).get("terminated") or {}
                if t.get("reason"): reasons.append(t["reason"])
                lt = (cs.get("lastState") or {}).get("terminated") or {}
                if lt.get("reason"): reasons.append(lt["reason"])
            if any(pat.search(r) for r in reasons):
                return True
        time.sleep(5)
    return False


def wait_for_restart_count(label: str, n: int, timeout: int = 240) -> int:
    """Block until at least one pod matching `label` has restartCount >= n.
    Returns the observed restartCount (>=n) on success, -1 on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            out = kubectl("get", "pods", "-n", TARGET_NS, "-l", label,
                          "-o", "json", check=False, timeout=15)
            data = json.loads(out) if out.strip() else {"items": []}
        except Exception:
            time.sleep(3)
            continue
        for p in data.get("items", []):
            for cs in p.get("status", {}).get("containerStatuses") or []:
                rc = int(cs.get("restartCount") or 0)
                if rc >= n:
                    return rc
        time.sleep(5)
    return -1


def trigger_poller_job(tag: str) -> str:
    job = f"loadtest-{tag}-{int(time.time())}"
    kubectl("create", "job", "-n", HEALER_NS, job,
            f"--from=cronjob/{CRONJOB}")
    return job


def wait_for_job(job: str, timeout: int = 240) -> bool:
    res = subprocess.run(
        ["kubectl", "wait", "-n", HEALER_NS,
         "--for=condition=complete", f"job/{job}",
         f"--timeout={timeout}s"],
        capture_output=True, text=True,
    )
    return res.returncode == 0


# ─── ConfigMap patch (raise rate limits) ───────────────────────────────────

def backup_and_patch_configmap(backup_path: Path) -> None:
    raw = kubectl("get", "configmap", CONFIGMAP, "-n", HEALER_NS, "-o", "json")
    cm = json.loads(raw)
    cfg_text = cm["data"]["config.yaml"]
    # Save just the original config.yaml text, not the whole resource
    # (resourceVersion would be stale by the time we restore).
    backup_path.write_text(cfg_text)
    info(f"Backed up config.yaml content → {backup_path}")

    new_text = re.sub(r"max_actions_per_hour:\s*\d+",
                      "max_actions_per_hour: 200", cfg_text)
    new_text = re.sub(r"cooldown_seconds:\s*\d+",
                      "cooldown_seconds: 5", new_text)
    # Enforce Haiku/Sonnet routing — Opus has tight per-minute rate limits
    # (10k input tokens/min) that cause iterations to error out under load.
    new_text = re.sub(r"^(\s*)model:\s*\S+",
                      r"\1model: claude-haiku-4-5-20251001",
                      new_text, count=1, flags=re.MULTILINE)
    if "incident_model:" in new_text:
        new_text = re.sub(r"^(\s*)incident_model:\s*\S+",
                          r"\1incident_model: claude-sonnet-4-6",
                          new_text, flags=re.MULTILINE)
    else:
        # Insert incident_model right after model: line if missing
        new_text = re.sub(
            r"^(\s*)(model:\s*claude-haiku-4-5-20251001)",
            r"\1\2\n\1incident_model: claude-sonnet-4-6",
            new_text, count=1, flags=re.MULTILINE)
    if new_text == cfg_text:
        warn("ConfigMap patch had no effect (regex didn't match) — "
             "results may be guardrail-blocked.")
    patch = {"data": {"config.yaml": new_text}}
    kubectl("patch", "configmap", CONFIGMAP, "-n", HEALER_NS,
            "--type=merge", "-p", json.dumps(patch))
    ok("Patched ConfigMap: triage=haiku-4.5, incident=sonnet-4.6, "
       "max_actions_per_hour=200, cooldown_seconds=5")


def restore_configmap(backup_path: Path) -> None:
    if not backup_path.exists():
        warn(f"No backup at {backup_path}; configmap NOT restored.")
        return
    original = backup_path.read_text()
    patch = {"data": {"config.yaml": original}}
    kubectl("patch", "configmap", CONFIGMAP, "-n", HEALER_NS,
            "--type=merge", "-p", json.dumps(patch))
    ok(f"Restored ConfigMap {CONFIGMAP} from {backup_path}")


# ─── Audit reader Deployment (queried only at start and end) ───────────────

def _reader_yaml() -> str:
    return textwrap.dedent(f"""\
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: {READER_DEPLOY}
          namespace: {HEALER_NS}
        spec:
          replicas: 1
          selector:
            matchLabels: {{app: {READER_DEPLOY}}}
          template:
            metadata:
              labels: {{app: {READER_DEPLOY}}}
            spec:
              serviceAccountName: {SA}
              securityContext:
                runAsUser: 10001
                runAsGroup: 10001
                fsGroup: 10001
              containers:
              - name: reader
                image: python:3.12-slim
                command: ["sleep", "infinity"]
                volumeMounts:
                - name: audit
                  mountPath: /data
                  readOnly: true
              volumes:
              - name: audit
                persistentVolumeClaim:
                  claimName: {PVC}
                  readOnly: true
        """)


def reader_up() -> None:
    _apply_yaml(_reader_yaml())
    info("Waiting for audit-reader pod to be Ready...")
    kubectl("wait", "-n", HEALER_NS,
            "--for=condition=Available",
            f"deployment/{READER_DEPLOY}", "--timeout=120s")
    ok("audit-reader Ready")


def reader_scale(replicas: int) -> None:
    kubectl("scale", "-n", HEALER_NS, f"deployment/{READER_DEPLOY}",
            f"--replicas={replicas}")
    if replicas == 0:
        # wait for pod gone so PVC is released for poller Jobs
        deadline = time.time() + 60
        while time.time() < deadline:
            out = kubectl("get", "pods", "-n", HEALER_NS,
                          "-l", f"app={READER_DEPLOY}",
                          "-o", "json", check=False)
            try:
                items = json.loads(out).get("items", [])
            except Exception:
                items = []
            if not items:
                return
            time.sleep(3)
        warn("audit-reader pod still present after 60s")
    else:
        kubectl("wait", "-n", HEALER_NS,
                "--for=condition=Available",
                f"deployment/{READER_DEPLOY}", "--timeout=120s", check=False)


def reader_down() -> None:
    kubectl("delete", "deployment", READER_DEPLOY, "-n", HEALER_NS,
            "--ignore-not-found=true", check=False, timeout=30)


def query_audit(sql: str) -> list[dict]:
    pyscript = (
        "import sqlite3,json,sys;"
        "c=sqlite3.connect('/data/audit.db');"
        "c.row_factory=sqlite3.Row;"
        f"rows=[dict(r) for r in c.execute({sql!r}).fetchall()];"
        "sys.stdout.write(json.dumps(rows,default=str))"
    )
    out = kubectl("exec", "-n", HEALER_NS, f"deployment/{READER_DEPLOY}",
                  "--", "python3", "-c", pyscript, timeout=60)
    return json.loads(out) if out.strip() else []


# ─── Iteration ─────────────────────────────────────────────────────────────

@dataclass
class IterationResult:
    iter: int
    scenario: str
    name: str
    t_apply: float = 0.0
    t_pod_failed: float = 0.0
    t_job_start: float = 0.0
    t_job_end: float = 0.0
    pod_reached_failure: bool = False
    job_completed: bool = False
    job_name: str = ""
    error: str = ""
    # filled in at end from audit DB
    audit_id: int | None = None
    action_taken: str = ""
    outcome: str = ""
    tokens_used: int = 0
    models_used: str = ""
    decision_time_s: float = 0.0
    cost_usd: float = 0.0
    expected_action_match: bool | None = None


def fetch_job_logs(job: str) -> str:
    res = subprocess.run(
        ["kubectl", "logs", "-n", HEALER_NS, "-l", f"job-name={job}",
         "--tail=-1"],
        capture_output=True, text=True, timeout=30,
    )
    return res.stdout if res.returncode == 0 else f"<log fetch failed: {res.stderr}>"


def run_iteration(i: int, scenario_id: str, run_id: str,
                  settle_s: int, logs_dir: Path) -> IterationResult:
    sc = SCENARIOS[scenario_id]
    name = f"{scenario_id.replace('_', '-')}-r{i}-{int(time.time())}"
    res = IterationResult(iter=i, scenario=scenario_id, name=name)
    info(f"[{i}] {scenario_id}: injecting {name}")

    try:
        res.t_apply = time.time()
        sc.inject(name, run_id)
    except Exception as e:
        res.error = f"inject failed: {e}"
        fail(f"[{i}] inject failed: {e}")
        return res

    label = sc.pod_label_template.format(name=name)
    if not wait_for_pod_state(label, sc.expected_failure_pattern, timeout=180):
        res.error = f"pod never reached {sc.expected_failure_pattern}"
        warn(f"[{i}] pod never reached {sc.expected_failure_pattern}")
        try: sc.cleanup(name)
        except Exception: pass
        return res
    res.t_pod_failed = time.time()
    res.pod_reached_failure = True
    ok(f"[{i}] pod {name} reached failure state "
       f"(+{res.t_pod_failed - res.t_apply:.0f}s)")

    # For cycling scenarios (CrashLoopBackOff, OOMKilled), wait until the
    # pod has crashed N times. Kubelet's exponential back-off means the
    # Waiting state lasts longer with each crash (10s, 20s, 40s, ...), so
    # by restartCount=2 the poller's snapshot reliably catches it.
    if sc.requires_restarts > 0:
        observed = wait_for_restart_count(label, sc.requires_restarts,
                                          timeout=240)
        if observed >= 0:
            ok(f"[{i}] pod cycled {observed} times (target {sc.requires_restarts})")
        else:
            warn(f"[{i}] pod never reached restartCount={sc.requires_restarts} "
                 f"in 240s; proceeding anyway")

    # Let the pod settle into a stable Waiting state so the poller's
    # snapshot reliably catches it.
    if settle_s > 0:
        time.sleep(settle_s)

    res.t_job_start = time.time()
    try:
        res.job_name = trigger_poller_job(scenario_id.replace("_", "-")[:20])
    except Exception as e:
        res.error = f"trigger_poller failed: {e}"
        fail(f"[{i}] trigger_poller failed: {e}")
        try: sc.cleanup(name)
        except Exception: pass
        return res

    completed = wait_for_job(res.job_name, timeout=240)
    res.t_job_end = time.time()
    res.job_completed = completed
    msg = "completed" if completed else "did not complete"
    info(f"[{i}] poller Job {res.job_name} {msg} "
         f"({res.t_job_end - res.t_job_start:.0f}s)")

    # Save the poller's logs for debugging — invaluable when an iteration
    # doesn't produce an audit row (was the pod even detected?).
    try:
        log_text = fetch_job_logs(res.job_name)
        (logs_dir / f"{res.job_name}.log").write_text(log_text)
    except Exception as e:
        warn(f"[{i}] could not fetch job logs: {e}")

    try:
        sc.cleanup(name)
    except Exception as e:
        warn(f"[{i}] cleanup error (non-fatal): {e}")
    return res


# ─── Aggregation ───────────────────────────────────────────────────────────

def attribute_audit_rows(results: list[IterationResult],
                         audit_rows: list[dict],
                         pricing: dict) -> None:
    """Match audit rows back to iterations by pod_name (which contains the
    iteration's unique deployment name as a prefix). Falls back to time-window
    matching: if no name match, attribute the row whose timestamp falls
    between t_job_start and t_job_end."""
    used_ids: set[int] = set()
    # Pre-parse audit row timestamps
    for a in audit_rows:
        ts = a.get("timestamp", "")
        try:
            a["_epoch"] = datetime.fromisoformat(
                ts.replace("Z", "+00:00")).timestamp() if ts else 0.0
        except Exception:
            a["_epoch"] = 0.0

    for r in results:
        # 1) prefer pod_name prefix match
        matches = [a for a in audit_rows
                   if a.get("pod_name", "").startswith(r.name)
                   and a.get("id") not in used_ids]
        # 2) fallback: any row whose timestamp falls within the iteration's job window
        if not matches:
            matches = [a for a in audit_rows
                       if r.t_job_start <= a.get("_epoch", 0) <= r.t_job_end + 5
                       and a.get("id") not in used_ids]
        if not matches:
            continue
        a = max(matches, key=lambda x: x.get("id", 0))
        used_ids.add(a.get("id"))
        r.audit_id = a.get("id")
        r.action_taken = a.get("action_taken", "") or ""
        r.outcome = a.get("outcome", "") or ""
        r.tokens_used = int(a.get("tokens_used") or 0)
        r.models_used = a.get("models_used", "") or ""
        r.decision_time_s = max(0.0, r.t_job_end - r.t_job_start - 12.0)
        r.cost_usd = compute_cost(r.tokens_used, r.models_used, pricing)
        sc = SCENARIOS.get(r.scenario)
        if sc:
            r.expected_action_match = r.action_taken in sc.expected_actions


def compute_cost(tokens: int, models_used: str, pricing: dict) -> float:
    if tokens <= 0:
        return 0.0
    models = [m.strip() for m in (models_used or "").split(",") if m.strip()]
    rates = [pricing[m]["blended_per_1m"] for m in models if m in pricing]
    if not rates:
        # Unknown model — fall back to haiku-rate as a lower bound
        first = next(iter(pricing.values()), {"blended_per_1m": 1.40})
        rate = first["blended_per_1m"]
    else:
        rate = max(rates)  # worst-case attribution
    return tokens / 1_000_000 * rate


def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs_sorted = sorted(xs)
    k = (len(xs_sorted) - 1) * p
    f = int(k)
    c = min(f + 1, len(xs_sorted) - 1)
    return xs_sorted[f] + (xs_sorted[c] - xs_sorted[f]) * (k - f)


def print_summary(results: list[IterationResult]) -> None:
    total = len(results)
    matched = [r for r in results if r.audit_id is not None]
    success = [r for r in matched if r.outcome == "success"]
    escalated = [r for r in matched if r.outcome == "escalated"]
    blocked = [r for r in matched if r.outcome == "guardrail_blocked"]
    errored = [r for r in matched if r.outcome == "error"]
    no_match = [r for r in results if r.audit_id is None]

    correct = [r for r in matched if r.expected_action_match]

    decision_times = [r.decision_time_s for r in matched if r.decision_time_s > 0]
    tokens = [r.tokens_used for r in matched if r.tokens_used > 0]
    costs = [r.cost_usd for r in matched if r.cost_usd > 0]

    def pct(n: int) -> str: return f"{100*n/total:.1f}%" if total else "n/a"

    print()
    print(_c("1", "Load test summary"))
    print("─" * 50)
    print(f"  Runs:                 {total}")
    print(f"  Matched audit rows:   {len(matched)}")
    print(f"  Unmatched (no row):   {len(no_match)}")
    print()
    print(f"  outcome=success:      {len(success):3d}  ({pct(len(success))})")
    print(f"  outcome=escalated:    {len(escalated):3d}  ({pct(len(escalated))})")
    print(f"  outcome=guardrail:    {len(blocked):3d}  ({pct(len(blocked))})")
    print(f"  outcome=error:        {len(errored):3d}  ({pct(len(errored))})")
    print()
    print(f"  Expected-action match: "
          f"{len(correct)}/{len(matched)} ({pct(len(correct))})")
    print()
    if decision_times:
        print(f"  Decision time (s):    "
              f"median {statistics.median(decision_times):6.1f}  "
              f"p90 {percentile(decision_times, 0.9):6.1f}  "
              f"max {max(decision_times):6.1f}")
        print(f"                        (wall-clock proxy: "
              f"job_runtime − 12s startup baseline)")
    if tokens:
        print(f"  Tokens / incident:    "
              f"median {int(statistics.median(tokens)):6d}  "
              f"avg {int(statistics.mean(tokens)):6d}  "
              f"total {sum(tokens):,}")
    if costs:
        print(f"  Cost / incident:      "
              f"median ${statistics.median(costs):.4f}  "
              f"avg ${statistics.mean(costs):.4f}  "
              f"total ${sum(costs):.2f}")
    print()
    # per-scenario
    print("  Per-scenario:")
    by_sc: dict[str, list[IterationResult]] = {}
    for r in matched:
        by_sc.setdefault(r.scenario, []).append(r)
    for sid in sorted(by_sc):
        rs = by_sc[sid]
        sc_correct = sum(1 for r in rs if r.expected_action_match)
        med_dt = statistics.median(
            [r.decision_time_s for r in rs if r.decision_time_s > 0] or [0])
        avg_cost = statistics.mean(
            [r.cost_usd for r in rs if r.cost_usd > 0] or [0])
        print(f"    {sid:22s} {sc_correct:3d}/{len(rs):3d} correct   "
              f"median {med_dt:5.1f}s   avg ${avg_cost:.4f}")
    print()


# ─── CSV output ────────────────────────────────────────────────────────────

CSV_FIELDS = [
    "iter", "scenario", "name", "job_name",
    "t_apply", "t_pod_failed", "t_job_start", "t_job_end",
    "pod_reached_failure", "job_completed",
    "audit_id", "action_taken", "outcome",
    "tokens_used", "models_used",
    "decision_time_s", "cost_usd",
    "expected_action_match", "error",
]


def write_csv(results: list[IterationResult], path: Path) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in results:
            row = asdict(r)
            w.writerow({k: row.get(k, "") for k in CSV_FIELDS})


# ─── Main ──────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--runs", type=int, default=50)
    ap.add_argument("--scenarios", default=",".join(SCENARIOS.keys()),
                    help="Comma-separated scenario ids to include in the pool.")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--patch-rate-limit", action=argparse.BooleanOptionalAction,
                    default=True, help="Patch the deployed configmap to raise "
                    "max_actions_per_hour for the duration of the run.")
    ap.add_argument("--cost-rates-json",
                    default=str(ROOT / "scripts" / "load_test_pricing.json"))
    ap.add_argument("--max-cost-usd", type=float, default=5.0,
                    help="Abort if running cost exceeds this.")
    ap.add_argument("--settle-seconds", type=int, default=10,
                    help="Sleep N seconds after pod reaches failure state "
                    "before triggering the poller. Reduces detection misses "
                    "on CrashLoopBackOff (which cycles).")
    ap.add_argument("--output-dir", default=str(ROOT / "results"))
    ap.add_argument("--yes", "-y", action="store_true",
                    help="Skip the Slack-noise confirmation prompt.")
    args = ap.parse_args()

    enabled = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    for s in enabled:
        if s not in SCENARIOS:
            fail(f"Unknown scenario: {s}. Available: {','.join(SCENARIOS)}")
            return 2
    if args.seed is not None:
        random.seed(args.seed)

    out_dir = Path(args.output_dir); out_dir.mkdir(parents=True, exist_ok=True)
    ts_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    csv_path = out_dir / f"results-{ts_stamp}.csv"
    logs_dir = out_dir / f"logs-{ts_stamp}"
    logs_dir.mkdir(parents=True, exist_ok=True)

    pricing = json.loads(Path(args.cost_rates_json).read_text())

    # ─── Pre-flight ────────────────────────────────────────────────────────
    info("Pre-flight checks...")
    try:
        kubectl("get", "cronjob", CRONJOB, "-n", HEALER_NS, timeout=15)
    except Exception as e:
        fail(f"CronJob {HEALER_NS}/{CRONJOB} not reachable: {e}")
        return 1
    ok(f"CronJob {HEALER_NS}/{CRONJOB} found")
    try:
        kubectl("get", "ns", TARGET_NS, timeout=15)
    except Exception as e:
        fail(f"Target namespace {TARGET_NS} not found: {e}"); return 1
    ok(f"Target namespace {TARGET_NS} found")

    # ─── Confirm ──────────────────────────────────────────────────────────
    if not args.yes:
        print()
        print(_c("1", f"This will run {args.runs} iterations against the live cluster."))
        print(f"  Scenarios:   {enabled}")
        print(f"  Slack noise: ~{args.runs} messages to the configured channel.")
        print(f"  Cost cap:    ${args.max_cost_usd:.2f}")
        ans = input("Continue? [y/N] ").strip().lower()
        if ans != "y":
            print("Aborted.")
            return 0

    run_id = uuid.uuid4().hex[:8]
    info(f"loadtest-run-id: {run_id}")
    backup_path = Path(f"/tmp/healer-config-bak-{run_id}.json")
    cm_patched = False
    reader_started = False
    initial_max_id = 0
    results: list[IterationResult] = []

    def restore_state():
        # Always run, even on Ctrl-C / errors
        try:
            if reader_started:
                reader_down()
                ok("audit-reader deleted")
        except Exception as e:
            warn(f"reader cleanup failed: {e}")
        try:
            if cm_patched:
                restore_configmap(backup_path)
        except Exception as e:
            warn(f"configmap restore failed: {e}")
        try:
            kubectl("delete", "deploy,pod", "-n", TARGET_NS, "-l",
                    f"loadtest-run={run_id}", "--ignore-not-found=true",
                    "--wait=false", check=False)
            ok("Final sweep of broken-app workloads")
        except Exception as e:
            warn(f"final sweep failed: {e}")

    def signal_handler(signum, _frame):
        warn(f"Received signal {signum}; tearing down.")
        restore_state()
        sys.exit(130)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        # ─── Setup ─────────────────────────────────────────────────────────
        if args.patch_rate_limit:
            backup_and_patch_configmap(backup_path)
            cm_patched = True

        reader_up()
        reader_started = True

        rows = query_audit(
            "SELECT COALESCE(MAX(id), 0) AS max_id FROM audit_log")
        initial_max_id = int(rows[0]["max_id"]) if rows else 0
        ok(f"audit_log watermark: max(id)={initial_max_id}")
        reader_scale(0)  # release the PVC for poller Jobs

        # ─── Run iterations ────────────────────────────────────────────────
        running_cost_estimate = 0.0
        for i in range(1, args.runs + 1):
            sid = random.choice(enabled)
            res = run_iteration(i, sid, run_id, args.settle_seconds, logs_dir)
            results.append(res)
            print()
            # Crude running cost estimate: assume ~5k tokens / iter at sonnet rate
            running_cost_estimate += 5000 / 1_000_000 * 6.0
            if running_cost_estimate > args.max_cost_usd:
                warn(f"Estimated running cost ${running_cost_estimate:.2f} "
                     f"exceeds cap ${args.max_cost_usd:.2f}; stopping early.")
                break

        # ─── Aggregate ─────────────────────────────────────────────────────
        info("Scaling audit-reader back up to query results...")
        reader_scale(1)
        all_rows = query_audit(
            f"SELECT * FROM audit_log WHERE id > {initial_max_id} "
            "ORDER BY id ASC")
        ok(f"Pulled {len(all_rows)} new audit rows")

        attribute_audit_rows(results, all_rows, pricing)
        write_csv(results, csv_path)
        ok(f"Wrote {csv_path}")

        # Dump raw audit rows for debugging (always, since it's small)
        dump_path = csv_path.with_suffix(".audit.json")
        dump_path.write_text(json.dumps(all_rows, indent=2, default=str))
        info(f"Raw audit rows → {dump_path}")
        unmatched = [r for r in results if r.audit_id is None]
        if unmatched and all_rows:
            warn(f"{len(unmatched)} iteration(s) had no matched audit row.")
            warn("Audit row pod_names actually recorded:")
            for a in all_rows:
                print(f"    id={a.get('id')} pod={a.get('pod_name')!r} "
                      f"action={a.get('action_taken')!r} "
                      f"outcome={a.get('outcome')!r}")
            warn("Iteration deployment names tried:")
            for r in unmatched:
                print(f"    iter={r.iter} name={r.name!r}")

        print_summary(results)

    finally:
        restore_state()

    return 0


if __name__ == "__main__":
    sys.exit(main())

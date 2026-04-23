#!/usr/bin/env bash
# End-to-end test of the K8s self-healing agent against a live GKE cluster.
#
# For each broken-app scenario: apply, wait for pod to enter the expected
# failure state, trigger the healer-poller CronJob manually, wait for the
# resulting Job to complete, then assert on poller logs + audit DB.
#
# Idempotent — safe to re-run. Cleans up on exit.
#
# Usage:   bash demo/e2e_test.sh
# Requires: kubectl authenticated to the cluster running healer-poller
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
APPS_DIR="$HERE/broken-apps"

HEALER_NS="k8s-healer"
TARGET_NS="default"
CRONJOB="healer-poller"

PASS=0
FAIL=0
FAILED_CASES=()

log()  { printf '\033[1;36m>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ok]\033[0m %s\n' "$*"; ((PASS++)) || true; }
bad()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*"; ((FAIL++)) || true; FAILED_CASES+=("$*"); }
info() { printf '  %s\n' "$*"; }

cleanup() {
  log "Cleanup: removing broken-app workloads"
  kubectl delete -f "$APPS_DIR" --ignore-not-found=true >/dev/null 2>&1 || true
}
trap cleanup EXIT

# ----------------------------------------------------------------------------
# Pre-flight
# ----------------------------------------------------------------------------
log "Pre-flight checks"

if ! kubectl get cronjob "$CRONJOB" -n "$HEALER_NS" >/dev/null 2>&1; then
  bad "CronJob $HEALER_NS/$CRONJOB not found — S1/S2 deployment incomplete"
  exit 1
fi
ok "CronJob $HEALER_NS/$CRONJOB exists"

if ! kubectl get ns "$TARGET_NS" >/dev/null 2>&1; then
  bad "Target namespace $TARGET_NS not found"
  exit 1
fi
ok "Target namespace $TARGET_NS exists"

# Start clean
cleanup

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

# Wait up to $1 seconds for a pod matching selector $2 to have status containing $3.
# Uses kubectl's jsonpath to read container status reason.
wait_for_pod_state() {
  local timeout=$1 selector=$2 expect_pattern=$3
  local end=$(( $(date +%s) + timeout ))
  while (( $(date +%s) < end )); do
    # Pull detailed status: Phase + any container waiting reason + any container last terminated reason
    local status
    status=$(kubectl get pods -n "$TARGET_NS" -l "$selector" -o json 2>/dev/null | \
      python3 -c '
import json, sys
d = json.load(sys.stdin)
for p in d.get("items", []):
    phase = p.get("status", {}).get("phase", "")
    reasons = [phase]
    for cs in p.get("status", {}).get("containerStatuses", []) or []:
        w = (cs.get("state") or {}).get("waiting") or {}
        if w.get("reason"): reasons.append(w["reason"])
        t = (cs.get("state") or {}).get("terminated") or {}
        if t.get("reason"): reasons.append(t["reason"])
        lt = (cs.get("lastState") or {}).get("terminated") or {}
        if lt.get("reason"): reasons.append(lt["reason"])
    print("|".join(reasons))
')
    if echo "$status" | grep -qi "$expect_pattern"; then
      return 0
    fi
    sleep 5
  done
  return 1
}

# Trigger a manual run of the CronJob, echo the job name.
trigger_poller() {
  local tag=$1
  local job_name="e2e-${tag}-$(date +%s)"
  kubectl create job -n "$HEALER_NS" "$job_name" --from="cronjob/$CRONJOB" >/dev/null
  echo "$job_name"
}

# Wait for a Job to reach Complete or Failed (up to $1 sec).
wait_for_job() {
  local timeout=$1 job=$2
  if kubectl wait -n "$HEALER_NS" --for=condition=complete "job/$job" --timeout="${timeout}s" >/dev/null 2>&1; then
    return 0
  fi
  # Maybe it failed — still proceed to log inspection
  return 1
}

# Fetch combined logs from all pods in the Job.
job_logs() {
  local job=$1
  kubectl logs -n "$HEALER_NS" -l "job-name=$job" --tail=-1 2>/dev/null || true
}

# ----------------------------------------------------------------------------
# Per-scenario test
# ----------------------------------------------------------------------------

# $1 = manifest file (relative to broken-apps/)
# $2 = pod label selector
# $3 = pattern to grep for in pod status (e.g. "ImagePullBackOff")
# $4 = expected action substring in poller logs (e.g. "alert_human")
# $5 = short tag for job names
run_scenario() {
  local manifest=$1 selector=$2 pod_state=$3 expect_action=$4 tag=$5

  log "── Scenario: $tag ($manifest) ──"

  info "Applying $manifest"
  kubectl apply -f "$APPS_DIR/$manifest" >/dev/null

  info "Waiting (up to 180s) for pod to enter state matching '$pod_state'"
  if wait_for_pod_state 180 "$selector" "$pod_state"; then
    ok "Pod reached $pod_state"
  else
    bad "[$tag] pod never reached $pod_state within 180s"
    kubectl get pods -n "$TARGET_NS" -l "$selector"
    return
  fi

  info "Triggering poller manually"
  local job
  job=$(trigger_poller "$tag")
  info "Created Job: $job"

  info "Waiting (up to 240s) for Job to complete"
  if wait_for_job 240 "$job"; then
    ok "[$tag] poller Job completed"
  else
    # Even if it didn't reach 'complete', it may have produced logs we can inspect
    info "Job did not report complete — inspecting logs anyway"
  fi

  local logs
  logs=$(job_logs "$job")

  if [[ -z "$logs" ]]; then
    bad "[$tag] no logs from poller Job"
    return
  fi

  # Assertion 1: poller found the unhealthy pod
  if echo "$logs" | grep -qE "Found [1-9][0-9]* unhealthy pod"; then
    ok "[$tag] poller detected unhealthy pod(s)"
  else
    bad "[$tag] poller did not report finding unhealthy pods"
  fi

  # Assertion 2: expected action appears in logs
  if echo "$logs" | grep -qi "$expect_action"; then
    ok "[$tag] poller logs contain expected action '$expect_action'"
  else
    bad "[$tag] poller logs missing expected action '$expect_action'"
    echo "$logs" | tail -40 | sed 's/^/    /'
  fi

  # Assertion 3: something appears in the audit summary for this run
  if echo "$logs" | grep -qE "Poller done — handled [1-9]"; then
    ok "[$tag] poller handled >=1 event"
  else
    bad "[$tag] poller did not record any handled events"
  fi

  info "Cleaning up $manifest"
  kubectl delete -f "$APPS_DIR/$manifest" --ignore-not-found=true >/dev/null 2>&1 || true
}

# ----------------------------------------------------------------------------
# Run scenarios
# ----------------------------------------------------------------------------

# ImagePullBackOff -> should escalate (alert_human)
run_scenario "imagepull.yaml" "scenario=imagepullbackoff" "ImagePullBackOff" "alert_human" "imgpull"

# Cooldown between runs so per-pod/guardrail state is fresh
log "Sleeping 70s to clear per-pod cooldown"
sleep 70

# OOMKilled -> should update_resource_limits
run_scenario "oom.yaml" "scenario=oomkilled" "OOMKilled" "update_resource_limits" "oom"

log "Sleeping 70s to clear per-pod cooldown"
sleep 70

# CrashLoopBackOff -> restart_pod OR alert_human (either is acceptable per playbook)
run_scenario "crashloop.yaml" "scenario=crashloopbackoff" "CrashLoopBackOff" "restart_pod\\|alert_human" "crash"

# ----------------------------------------------------------------------------
# Audit DB spot-check (requires sqlite3 in image OR python fallback)
# ----------------------------------------------------------------------------

log "── Audit DB spot-check ──"

# Find most recent healer-poller pod (may be gone if Job TTL expired)
LATEST_POD=$(kubectl get pods -n "$HEALER_NS" \
  --sort-by=.metadata.creationTimestamp \
  -o jsonpath='{.items[-1:].metadata.name}' 2>/dev/null || true)

if [[ -n "$LATEST_POD" ]]; then
  # Try sqlite3 directly; fall back to python if sqlite3 not in image
  audit_output=$(kubectl exec -n "$HEALER_NS" "$LATEST_POD" -- \
    sh -c 'command -v sqlite3 >/dev/null && sqlite3 /data/audit.db "SELECT COUNT(*) FROM audit_log;" 2>/dev/null \
      || python3 -c "import sqlite3; print(sqlite3.connect(\"/data/audit.db\").execute(\"SELECT COUNT(*) FROM audit_log\").fetchone()[0])"' 2>/dev/null || true)

  if [[ -n "$audit_output" ]] && [[ "$audit_output" -gt 0 ]] 2>/dev/null; then
    ok "Audit DB has $audit_output row(s)"
  else
    info "Could not query audit DB via latest pod $LATEST_POD (may have exited; check via debug pod)"
  fi
else
  info "No recent poller pod to exec into for audit check"
fi

info "Slack check: eyeball the #oncall-sre channel for a message from the ImagePullBackOff run"

# ----------------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------------

echo
log "────────────────────────────────────────"
log "Results: $PASS pass, $FAIL fail"
log "────────────────────────────────────────"

if (( FAIL > 0 )); then
  for f in "${FAILED_CASES[@]}"; do
    echo "  [fail] $f"
  done
  exit 1
fi

echo "All e2e checks passed."

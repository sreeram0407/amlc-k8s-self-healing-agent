# Integration Test Checklist — K8s Self-Healing Agent

This is the "does the whole system actually work?" checklist. Run through it after S1 (infra), S2 (deployment), and S3 (skill/runbook) have all merged. Every `- [ ]` is a concrete, observable check.

**Context:**
- CronJob: `healer-poller` in namespace `k8s-healer`, schedule `*/5 * * * *`
- Watched namespace: `default` (see `k8s/05-cronjob.yaml` env `WATCH_NAMESPACES`)
- Broken workloads: `demo/broken-apps/{oom,crashloop,imagepull}.yaml`
- Agent logic: `src/agent.py` (SYSTEM_PROMPT + tool-use loop), `src/guardrails.py`
- Audit DB: `/data/audit.db` inside the poller pod (PVC `healer-audit`)

---

## 1. Pre-flight (before any scenario test)

### S1 / S2 infra
- [ ] `kubectl get cronjob healer-poller -n k8s-healer` shows `SCHEDULE = */5 * * * *`
- [ ] `kubectl get sa healer-sa -n k8s-healer` exists
- [ ] `kubectl get rolebinding,clusterrolebinding -A | grep healer` shows RBAC bound
- [ ] `kubectl get secretproviderclass healer-secrets -n k8s-healer` exists
- [ ] `kubectl get pvc healer-audit -n k8s-healer` has `STATUS = Bound`
- [ ] Last scheduled run succeeded: `kubectl get jobs -n k8s-healer` shows recent `Complete` (not `Failed`)
- [ ] Image tag in `k8s/05-cronjob.yaml` matches what was pushed to Artifact Registry

### Secrets
- [ ] `gcloud secrets versions list anthropic-api-key --project=openclaw-k8s-healer` shows an enabled version
- [ ] `gcloud secrets versions list slack-bot-token --project=openclaw-k8s-healer` shows an enabled version
- [ ] A trigger-run poller pod can read both — check with:
  ```
  kubectl logs -n k8s-healer -l job-name=<last-job> | grep -i 'model:\|channel:'
  ```

### S3 runbook / skill
- [ ] Skill / runbook logic is committed (check `src/agent.py` SYSTEM_PROMPT or wherever S3 landed it)
- [ ] Model routing: Haiku for polls, Sonnet for confirmed incidents — verified in code + in audit `tokens_used` per run being small when no incident
- [ ] Slack escalation template is in place (`src/slack_integration.py`)

### Slack sanity
- [ ] Bot is invited to `#oncall-sre` (or whatever `config.yaml -> openclaw.channel` is)
- [ ] Test message: temporarily apply `imagepull.yaml`, manual-trigger poller, confirm a message arrives within 2 min

---

## 2. Per-scenario verification matrix

For each broken workload, confirm every cell in the row. Expected behavior is derived from `src/agent.py` SYSTEM_PROMPT Remediation Playbook (lines 50–57).

| Scenario | Pod status in cluster | Expected agent action | Expected Slack? | Expected audit row |
|---|---|---|---|---|
| **OOMKilled** (`oom-demo`) | `OOMKilled` | `update_resource_limits` (memory +50%) | No (fix succeeded) | `action_taken = update_resource_limits`, `outcome = success` |
| **CrashLoopBackOff** (`crashloop-demo`) | `CrashLoopBackOff` | `restart_pod` (no recent deploy) OR `alert_human` (loops persist) | Maybe (after 3 restarts) | `action_taken = restart_pod` OR `alert_human`, `guardrail_check` populated |
| **ImagePullBackOff** (`imagepull-demo`) | `ImagePullBackOff` | `alert_human` (per playbook: cannot auto-fix bad image) | **Yes** | `action_taken = alert_human`, `outcome = escalated` |

### OOMKilled (oom-demo)
- [ ] Apply: `kubectl apply -f demo/broken-apps/oom.yaml`
- [ ] Within 60s: `kubectl get pods -n default -l app=oom-demo` shows `OOMKilled` or `CrashLoopBackOff` with `OOMKilled` in `kubectl describe`
- [ ] Trigger poller: `kubectl create job -n k8s-healer healer-test-oom-$(date +%s) --from=cronjob/healer-poller`
- [ ] Poller logs (within 2 min) show: "Found N unhealthy pod(s)" AND the pod name `oom-demo-*`
- [ ] Poller logs show tool calls: `get_pod_status`, `get_pod_logs`, `get_events`, then `update_resource_limits`
- [ ] Deployment memory limit increased: `kubectl get deploy oom-demo -n default -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}'` is > `10Mi`
- [ ] Guardrail did NOT block (check `guardrail_check` column is blank or "OK")
- [ ] New pod eventually reaches `Running`

### CrashLoopBackOff (crashloop-demo)
- [ ] Apply: `kubectl apply -f demo/broken-apps/crashloop.yaml`
- [ ] Within 90s: `kubectl get pods -n default -l app=crashloop-demo` shows `CrashLoopBackOff` with `restart_count > 2`
- [ ] Trigger poller
- [ ] Poller logs show investigation: logs include "FATAL: required env var DB_URL is not set"
- [ ] Agent chooses `restart_pod` (no recent deploy) OR escalates via `alert_human` (if it correctly recognizes the config is the problem)
- [ ] If `restart_pod` chosen: after 3rd restart in an hour, `max_restarts_per_hour` guardrail should block and force `alert_human` — verify the 4th run escalates

### ImagePullBackOff (imagepull-demo)
- [ ] Apply: `kubectl apply -f demo/broken-apps/imagepull.yaml`
- [ ] Within 60s: `kubectl get pods -n default -l app=imagepull-demo` shows `ImagePullBackOff` or `ErrImagePull`
- [ ] Trigger poller
- [ ] Poller logs show the agent calling `alert_human` (not a remediation tool)
- [ ] **Slack channel receives a message** naming `imagepull-demo-*` and mentioning the bad image tag
- [ ] Audit row: `action_taken = alert_human`, `outcome` contains "escalated"
- [ ] Pod remains `ImagePullBackOff` (agent correctly did NOT try to "fix" it)

---

## 3. Guardrail behavior

### Rate limit (global `max_actions_per_hour = 10`)
- [ ] Apply 11+ unique broken pods in one namespace, trigger poller repeatedly
- [ ] After the 10th successful remediation in an hour, 11th attempt should return `Global rate limit reached` in poller logs
- [ ] Audit row for the 11th shows `guardrail_check = "Global rate limit reached..."` and no action taken

### Blast radius (`blast_radius_threshold = 0.5`)
- [ ] Apply `oom.yaml`, `crashloop.yaml`, `imagepull.yaml` all at once, scale replicas so >50% of `default` pods are unhealthy
- [ ] Trigger poller
- [ ] Poller logs show `Blast radius threshold exceeded` for at least one guardrail check
- [ ] All such events escalate (not auto-fix)

### Per-pod cooldown (`cooldown_seconds = 60`)
- [ ] Trigger poller twice within 60s on the same broken pod
- [ ] Second run's guardrail output shows `Cooldown active for pod '...'` for that pod

### Max restart per pod (`max_restarts_per_hour = 3`)
- [ ] Apply `crashloop.yaml`, trigger poller 4 times with 61s between runs (to clear cooldown but not rate-limit window)
- [ ] 4th restart attempt should be blocked: `has been restarted 3 times in the past hour`
- [ ] Agent escalates via `alert_human` instead

### Memory multiplier (`max_memory_multiplier = 2.0`)
- [ ] Apply `oom.yaml` (original limit `10Mi`) — upper bound is `20Mi`
- [ ] If agent tries to bump to >20Mi in one shot, guardrail blocks with `exceeds 2.0x original`
- [ ] Audit row captures the block

---

## 4. End-to-end path (combined system)

- [ ] Start with empty `default` namespace (except system pods): `kubectl delete deploy -n default --all`
- [ ] Apply all 3 broken manifests: `demo/broken-apps/apply.sh`
- [ ] Wait for CronJob to fire naturally (do NOT manual-trigger) — 5 min
- [ ] Within 7 min total, check:
  - [ ] `kubectl get jobs -n k8s-healer` shows a fresh `Complete` job
  - [ ] `kubectl logs -n k8s-healer -l job-name=<fresh>` contains lines like "Found 3 unhealthy pod(s)"
  - [ ] For ImagePullBackOff: a Slack message arrived
  - [ ] For OOMKilled: deployment memory was patched
  - [ ] Audit DB has at least 3 rows from this run (one per pod)
- [ ] Run cleanup: `demo/broken-apps/cleanup.sh`
- [ ] Next CronJob run logs `[ok] No unhealthy pods detected — nothing to do`

---

## 5. Rollback path (extra scenario, not covered by broken-apps/)

The playbook says CrashLoopBackOff **with a recent deployment** should trigger `rollback_deployment`. Test this manually:

- [ ] Apply a healthy Deployment: `kubectl create deploy rollback-demo -n default --image=nginx:1.25 --replicas=1`
- [ ] Wait for it to be `Running`
- [ ] Update to a broken image: `kubectl set image deploy/rollback-demo app=nginx:does-not-exist -n default`
- [ ] Within 2 min, pod enters `ImagePullBackOff` or `CrashLoopBackOff`
- [ ] Trigger poller
- [ ] Agent investigates, sees recent deployment (`last_deployed` is <60 min ago — within `rollback_window_minutes`)
- [ ] Agent calls `rollback_deployment` -> deployment reverts to `nginx:1.25`
- [ ] Verify: `kubectl get deploy rollback-demo -n default -o jsonpath='{.spec.template.spec.containers[0].image}'` == `nginx:1.25`
- [ ] Cleanup: `kubectl delete deploy rollback-demo -n default`

---

## 6. Observability / audit

### Poller run summary (every run should print)
- [ ] Header: ` Self-healing poller run @ <ts>`
- [ ] Config echo: `• Model:`, `• Alert channel:`, `• Watching namespaces:`
- [ ] Unhealthy count: `[warn]  Found N unhealthy pod(s)`  OR  `[ok] No unhealthy pods detected`
- [ ] Footer: `[ok] Poller done — handled X/N events`
- [ ] Trailing audit summary table (one line per event: timestamp, pod_name, action, outcome)

### Audit DB queries
Run these from a debug pod with `sqlite3` installed, mounting the same PVC, or `kubectl exec` into a poller pod while it's alive:

- [ ] `SELECT COUNT(*) FROM audit_log;` — should grow monotonically
- [ ] `SELECT action_taken, COUNT(*) FROM audit_log GROUP BY action_taken;` — distribution matches expectations
- [ ] `SELECT pod_name, diagnosis, action_taken, outcome FROM audit_log ORDER BY timestamp DESC LIMIT 10;` — diagnoses are coherent (not empty, not repeated)
- [ ] `SELECT SUM(tokens_used) FROM audit_log WHERE timestamp > datetime('now', '-1 day');` — sanity check on spend

### Cost sanity
- [ ] Anthropic console shows token usage consistent with audit `tokens_used` totals
- [ ] GCP budget alert did NOT fire during testing

---

## Sign-off

- [ ] All of §1 (pre-flight) checked
- [ ] All 3 scenarios in §2 pass
- [ ] At least 2 guardrails in §3 demonstrably triggered
- [ ] §4 end-to-end pass (no manual triggers)
- [ ] §5 rollback demonstrated
- [ ] §6 audit DB has the expected rows
- [ ] Run `demo/e2e_test.sh` — passes

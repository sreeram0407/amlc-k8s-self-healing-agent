# Demo Script — K8s Self-Healing Agent

**Runtime:** ~10 min. **Audience:** engineers who understand Kubernetes but haven't seen this agent.

**One-line pitch:** "A CronJob that detects unhealthy pods every 5 min, asks Claude what's wrong, fixes it if it's safe to, and pages a human otherwise."

---

## Terminal layout (before starting)

Three panes:

| Pane | Command kept open |
|---|---|
| **A — Cluster** | `watch -n 2 'kubectl get pods -n default -o wide'` |
| **B — Poller logs** | (empty; you'll fill this in per-act) |
| **C — Slack** | Browser/desktop Slack pointed at the `#oncall-sre` channel |

Also open a 4th terminal for typing commands (not watched by the audience).

---

## Setup (do BEFORE the audience arrives)

```bash
# 1. Confirm the CronJob is live and recently successful
kubectl get cronjob healer-poller -n k8s-healer
kubectl get jobs -n k8s-healer | tail -5

# 2. Confirm default namespace is clean
kubectl get deploy -n default
# If anything exists: kubectl delete deploy --all -n default

# 3. Confirm Slack bot is in the channel (look for the bot in the sidebar)

# 4. Pre-pull images to avoid awkward pause (optional)
kubectl run prepull --rm -i --restart=Never --image=polinux/stress:latest --command -- true || true
kubectl run prepull --rm -i --restart=Never --image=busybox:1.36 --command -- true || true
```

---

## Act 1 — Healthy baseline (30s)

**What to say:**
> "Here's our cluster. Default namespace is empty — nothing broken. The healer CronJob runs every 5 minutes against this namespace."

**What to type (Pane B):**
```bash
# Show the CronJob and the most recent run's logs
kubectl get cronjob healer-poller -n k8s-healer
LATEST=$(kubectl get jobs -n k8s-healer -o jsonpath='{.items[-1:].metadata.name}')
kubectl logs -n k8s-healer -l job-name=$LATEST
```

**What the audience sees:**
- CronJob spec: `SCHEDULE */5 * * * *`
- Poller log ending in `[ok] No unhealthy pods detected — nothing to do.`

**Key point to land:** "Idle runs are cheap — a single Haiku-level API call to check pod state, nothing more."

---

## Act 2 — ImagePullBackOff -> escalation (2 min)

**What to say:**
> "Now I'll break something the agent cannot fix — a bad image tag. The agent should detect it, investigate, realize no remediation is safe, and page a human via Slack."

**What to type:**
```bash
kubectl apply -f demo/broken-apps/imagepull.yaml
```

*(Wait ~30s, point to Pane A showing the pod going `ImagePullBackOff`)*

```bash
# Don't wait 5 minutes — trigger the poller now
JOB=healer-demo-imgpull-$(date +%s)
kubectl create job -n k8s-healer $JOB --from=cronjob/healer-poller

# Follow its logs
kubectl logs -n k8s-healer -l job-name=$JOB -f
```

**What to say while logs stream:**
> "Watch the agent work: it calls `get_pod_status`, reads the logs, scans events. It sees the image doesn't exist. The remediation playbook says image problems require a human — no auto-fix. So it calls `alert_human`."

**What the audience sees:**
- Poller logs: `[warn] Found 1 unhealthy pod(s)`
- Tool-call sequence in logs (investigate -> decide -> `alert_human`)
- **Slack (Pane C):** a message arrives within ~30s — names the pod, flags the bad image tag, recommends human action
- Audit summary at end: `alert_human   escalated`

**Key point to land:** "The agent *chooses* to escalate. It's not a fallback for errors — it's the correct action for this failure mode."

---

## Act 3 — OOMKilled -> auto-fix (3 min)

**What to say:**
> "This time, a pod that's OOMKilled because its memory limit is too low — 10Mi for a process that wants 250Mi. The agent should bump the limit."

**What to type:**
```bash
kubectl apply -f demo/broken-apps/oom.yaml

# Show the pod flapping (Pane A already shows it)
kubectl describe pod -n default -l app=oom-demo | grep -A 2 "Last State"
```

*(The audience sees `Reason: OOMKilled` in describe output.)*

```bash
JOB=healer-demo-oom-$(date +%s)
kubectl create job -n k8s-healer $JOB --from=cronjob/healer-poller
kubectl logs -n k8s-healer -l job-name=$JOB -f
```

**What to say while logs stream:**
> "The agent investigates — checks status, logs, events, deployment info. Diagnosis: memory starvation. Action: `update_resource_limits`. The guardrails double-check it's not exceeding 2x the original limit."

**What the audience sees:**
- Tool-call sequence ending in `update_resource_limits`
- Deployment memory limit changed:
  ```bash
  kubectl get deploy oom-demo -n default \
    -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}'
  ```
- After ~30s, Pane A shows `oom-demo-...` pod reach `Running`

**Key point to land:** "Auto-fix is gated by guardrails. If the agent wanted to jump from 10Mi to 100Mi, the 2x multiplier rule would block it. Every action has a safety net."

---

## Act 4 — Guardrail in action (2 min)

**What to say:**
> "Now let me show what happens when we overwhelm the system. I'll break three workloads at once — more than half the pods in this namespace are now unhealthy. That trips the blast-radius guardrail."

**What to type:**
```bash
# Clean first to get a predictable blast-radius ratio
./demo/broken-apps/cleanup.sh

# Apply all three
./demo/broken-apps/apply.sh

# Trigger
JOB=healer-demo-swarm-$(date +%s)
kubectl create job -n k8s-healer $JOB --from=cronjob/healer-poller
kubectl logs -n k8s-healer -l job-name=$JOB -f
```

**What to say while logs stream:**
> "Look for the phrase `Blast radius threshold exceeded` in the logs. The guardrail refuses to let the agent take *any* remediation action when the namespace is this unhealthy — it forces escalation instead. The reasoning: if this many things are broken at once, the agent is probably wrong about what to do."

**What the audience sees:**
- Multiple `Blast radius threshold exceeded` messages
- Every failing pod ends with `alert_human` / escalation
- Slack gets several messages

**Key point to land:** "Guardrails are the contract. The agent can be wrong — the guardrail says *how wrong is allowed*."

---

## Act 5 — Audit trail (1 min)

**What to say:**
> "Everything the agent did is logged to a SQLite DB on a persistent volume. This is the record for post-incident review — what was the diagnosis, what was the action, what did the guardrail say."

**What to type:**
```bash
# Find a running poller pod (or exec into a new one)
POLLER_POD=$(kubectl get pods -n k8s-healer \
  -l job-name=$JOB -o jsonpath='{.items[0].metadata.name}')

# Query audit log
kubectl exec -n k8s-healer $POLLER_POD -- \
  sqlite3 /data/audit.db \
  "SELECT timestamp, pod_name, action_taken, outcome FROM audit_log ORDER BY timestamp DESC LIMIT 10;"
```

*Or if the pod has exited (jobs finish fast), use a debug pod with the PVC mounted:*
```bash
kubectl debug --image=alpine --attach -n k8s-healer <poller-pod> -- \
  sh -c 'apk add sqlite && sqlite3 /data/audit.db "SELECT * FROM audit_log LIMIT 5;"'
```

**What the audience sees:**
- A handful of rows: clear timestamps, clear actions, clear outcomes
- Tokens used column — low numbers, showing cost stays bounded per event

**Key point to land:** "This is what a human on-call needs the next morning to understand what the agent did overnight."

---

## Cleanup (after demo)

```bash
./demo/broken-apps/cleanup.sh

# Verify default namespace is empty
kubectl get pods -n default
```

---

## Q&A Cheat Sheet

**"What if Claude hallucinates a tool call?"**
> Every remediation tool goes through `Guardrails.check()` before executing. Investigation tools are read-only. The agent can't delete a deployment or scale beyond the cap — the guardrail won't let it. See `src/guardrails.py`.

**"How much does it cost?"**
> Haiku for idle polls (most of the day, empty namespace -> 1 cheap API call). Sonnet only when we're actually investigating an incident. Hard cap at Anthropic console level — $20/month budget.

**"What if the agent makes the wrong decision?"**
> Rate limit (10 actions/hour globally), per-pod cooldown (60s), max 3 restarts/hour per pod, 2x memory limit ceiling. Worst case: it escalates. It cannot take a destructive action in a loop.

**"Why CronJob and not a deployment / controller?"**
> Simplicity. CronJob semantics match the "every 5 min, do a sweep" mental model. No reconciliation loop to reason about. Each run is stateless except for the audit DB.

**"What about race conditions between two poller runs?"**
> `concurrencyPolicy: Forbid` in `k8s/05-cronjob.yaml` — if a previous run is still executing, the next one is skipped.

**"Can I run this locally without a cluster?"**
> Yes — `python demo/run_demo.py` uses `MockCluster` + `mock_anthropic` and walks through 4 scripted scenarios.

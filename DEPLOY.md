# K8s Self-Healing Agent — GKE deployment

Your local self-healing agent, containerized and wired to:
- Real Kubernetes (via `kubernetes` Python client + in-cluster ServiceAccount)
- Real Slack alerts (via `slack_sdk`, reading `SLACK_BOT_TOKEN` from Secret Manager)
- Real Claude API (via `anthropic`, reading `ANTHROPIC_API_KEY` from Secret Manager)

## What changed from your local demo

**Added:**
- `src/k8s_cluster.py` — `KubernetesCluster` class, drop-in replacement for `MockCluster`
- `src/slack_integration.py` — `SlackIntegration` class, drop-in replacement for `OpenClawIntegration`
- `poller.py` — CronJob entrypoint (replaces `run_demo.py` for production)
- `Dockerfile`, `requirements.txt`
- `k8s/` manifests

**Unchanged (still works locally):**
- `src/agent.py`, `src/guardrails.py`, `src/mcp_server.py`, `src/audit.py`
- `src/mock_cluster.py`, `src/mock_anthropic.py` (local demo path)
- `src/openclaw_integration.py` (still used by `run_demo.py`)
- `demo/run_demo.py`, `demo/scenarios.py`

---

## Prerequisites (should all be done)

- [x] GKE cluster `openclaw-healer` (S1)
- [x] GCP SA `openclaw-agent` with `secretmanager.secretAccessor` (S1)
- [x] Workload Identity binding for `openclaw/openclaw-sa` (S1)
- [ ] **Add WI binding for our new SA** (step 1 below)
- [ ] Secret Manager secrets populated (step 2)
- [ ] Artifact Registry repo for the image (step 3)

---

## Step 1: Add Workload Identity binding for `k8s-healer/healer-sa`

S1 bound the GCP SA to `openclaw/openclaw-sa`. We need it bound to our new SA too:

```bash
gcloud iam service-accounts add-iam-policy-binding \
  openclaw-agent@openclaw-k8s-healer.iam.gserviceaccount.com \
  --role roles/iam.workloadIdentityUser \
  --member "serviceAccount:openclaw-k8s-healer.svc.id.goog[k8s-healer/healer-sa]" \
  --project openclaw-k8s-healer
```

## Step 2: Populate Secret Manager

```bash
# Anthropic API key
echo -n "sk-ant-REPLACE_ME" | \
  gcloud secrets create anthropic-api-key --data-file=- \
  --replication-policy=automatic --project=openclaw-k8s-healer

# Slack bot token (xoxb-...)
echo -n "xoxb-REPLACE_ME" | \
  gcloud secrets create slack-bot-token --data-file=- \
  --replication-policy=automatic --project=openclaw-k8s-healer

# Verify
gcloud secrets list --project=openclaw-k8s-healer
```

If the secrets already exist, use `versions add` instead of `create`:
```bash
echo -n "sk-ant-..." | gcloud secrets versions add anthropic-api-key --data-file=-
```

## Step 3: Build and push the image

Create an Artifact Registry repo (one time):
```bash
gcloud artifacts repositories create healer \
  --repository-format=docker \
  --location=us-central1 \
  --project=openclaw-k8s-healer

gcloud auth configure-docker us-central1-docker.pkg.dev
```

Build locally (or in Cloud Shell — it has Docker):
```bash
# From the project root (where Dockerfile lives)
docker build -t us-central1-docker.pkg.dev/openclaw-k8s-healer/healer/healer:latest .
docker push us-central1-docker.pkg.dev/openclaw-k8s-healer/healer/healer:latest
```

Or build with Cloud Build (no local Docker needed):
```bash
gcloud builds submit --tag us-central1-docker.pkg.dev/openclaw-k8s-healer/healer/healer:latest
```

## Step 4: Apply the manifests

```bash
kubectl apply -f k8s/01-namespace-sa.yaml
kubectl apply -f k8s/02-rbac.yaml
kubectl apply -f k8s/03-secretproviderclass.yaml
kubectl apply -f k8s/04-configmap-pvc.yaml
kubectl apply -f k8s/05-cronjob.yaml

kubectl get cronjob -n k8s-healer
```

## Step 5: Trigger a manual run (don't wait 5 min)

```bash
kubectl create job -n k8s-healer healer-manual-$(date +%s) \
  --from=cronjob/healer-poller

# Watch the pod
kubectl get pods -n k8s-healer -w
# Then view logs
kubectl logs -n k8s-healer -l job-name=healer-manual-... -f
```

## Step 6: Test the full loop

Apply a deliberately broken workload to the `default` namespace (S4's territory, but you can do this for your own sanity check):

```bash
# OOMKilled pod
kubectl run oom-test --image=polinux/stress --restart=Never -- \
  stress --vm 1 --vm-bytes 250M --vm-hang 1
kubectl set resources pod/oom-test --limits=memory=10Mi

# ImagePullBackOff pod
kubectl run bad-image --image=nginx:nonexistent-tag-12345 --restart=Never
```

Wait 5 min (or trigger manually again). You should see:
1. Agent logs list unhealthy pods
2. Agent investigates, decides, escalates to Slack (for ImagePullBackOff)
3. Slack channel gets a message

---

## Troubleshooting

**"403 Forbidden" accessing Secret Manager:**
- Check WI binding (step 1)
- Check the GCP SA has `roles/secretmanager.secretAccessor`

**"ImagePullBackOff" on the healer pod itself:**
- Verify you pushed to the right Artifact Registry path
- `gcloud auth configure-docker us-central1-docker.pkg.dev` from your build machine

**"403" from the Slack API:**
- Bot token needs `chat:write` scope
- Bot must be invited to `#k8s-alerts` (run `/invite @your-bot` in the channel)

**Anthropic API errors:**
- If the spend cap ($20 from S4's plan) is hit, calls will fail
- Check console.anthropic.com → Usage

**Pods not detected as unhealthy:**
- Check `WATCH_NAMESPACES` env var in the CronJob — defaults to `default` only
- Remove or expand to cover where S4 is applying broken workloads

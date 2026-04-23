#!/usr/bin/env bash
# Apply the three broken-app demo manifests to the `default` namespace.
# Selecting one: ./apply.sh oom | crashloop | imagepull
# No arg = apply all three.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

SCENARIOS=("oom" "crashloop" "imagepull")
if [[ $# -gt 0 ]]; then
  SCENARIOS=("$@")
fi

for s in "${SCENARIOS[@]}"; do
  f="${s}.yaml"
  if [[ ! -f "$f" ]]; then
    echo "!! Unknown scenario: $s (expected one of oom, crashloop, imagepull)"
    exit 1
  fi
  echo "── Applying $f"
  kubectl apply -f "$f"
done

echo
echo "── Broken workloads applied. Watch them flip unhealthy:"
echo "   kubectl get pods -n default -l demo=broken -w"
echo
echo "── The healer CronJob runs every 5 min. To trigger now:"
echo "   kubectl create job -n k8s-healer healer-manual-\$(date +%s) --from=cronjob/healer-poller"

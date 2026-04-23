#!/usr/bin/env bash
# Delete all broken-app demo workloads from the `default` namespace.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

echo "── Deleting broken-app manifests (oom, crashloop, imagepull)"
kubectl delete -f . --ignore-not-found=true

echo
echo "── Verifying (should print 'No resources found'):"
kubectl get pods -n default -l demo=broken 2>&1 | tail -n 5

#!/usr/bin/env bash
# Scan a local image with Trivy, used by CI and by the release workflow.
#   .github/scripts/trivy-scan.sh <image>
#
# Fails on HIGH or CRITICAL vulnerabilities that have a fix, and on secrets.
# Accepted findings are in .trivyignore (each with a reason and an expiry date).
#
# Trivy's own release channels were compromised in March 2026 (CVE-2026-33634),
# so no trivy-action: the release tarball is pinned by SHA-256, taken from a
# checksums file whose cosign signature was verified against Trivy's release
# workflow.
set -euo pipefail

image="${1:?usage: trivy-scan.sh <image>}"
repo="$(cd "$(dirname "$0")/../.." && pwd)"

TRIVY_VERSION="0.75.0"
TRIVY_SHA256="c6e65abddb348e25f10549df887045629cf28cc72453cd1c63acb717316b3f3f"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cd "$work"
curl -fsSLo trivy.tar.gz \
  "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz"
echo "${TRIVY_SHA256}  trivy.tar.gz" | sha256sum --check --strict
tar -xzf trivy.tar.gz trivy
./trivy --version
./trivy image --scanners vuln,secret --severity HIGH,CRITICAL \
  --ignore-unfixed --exit-code 1 --no-progress \
  --ignorefile "$repo/.trivyignore" "$image"

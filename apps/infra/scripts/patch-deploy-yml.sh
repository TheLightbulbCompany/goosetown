#!/usr/bin/env bash
# Pin aws-actions/aws-cloudformation-github-deploy to a known-good version.
#
# cdk-pipelines-github hardcodes ``@v2`` (floating major) in pipeline.js. The
# action's v2.2.0 release on 2026-04-27 added event polling that calls
# ``cloudformation:DescribeEvents`` — a permission the cdk-bootstrap deploy
# role does NOT grant. NO_CHANGES stacks fail the deploy on the polling call.
#
# Until the upstream library exposes a version-override config (or our deploy
# role is reissued with DescribeEvents), pin to v2.1.0. Run this after every
# ``cdk synth`` so the committed workflow file matches what we want.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKFLOW_FILE="${SCRIPT_DIR}/../../../.github/workflows/deploy.yml"

if [[ ! -f "$WORKFLOW_FILE" ]]; then
  echo "::error::deploy.yml not found at $WORKFLOW_FILE — run 'cdk synth' first."
  exit 1
fi

# Pin only the floating ``@v2`` reference. Leave any explicit v2.x.x pins alone.
# The action ref appears only as ``aws-actions/aws-cloudformation-github-deploy@v2`` in
# the generated workflow, so a literal find-and-replace is safe.
if grep -q 'aws-cloudformation-github-deploy@v2$' "$WORKFLOW_FILE" \
   || grep -q 'aws-cloudformation-github-deploy@v2[^.]' "$WORKFLOW_FILE"; then
  # ``-i ''`` for BSD sed (macOS); GNU sed accepts the same form.
  sed -i.bak 's|aws-cloudformation-github-deploy@v2$|aws-cloudformation-github-deploy@v2.1.0|g' "$WORKFLOW_FILE"
  rm -f "${WORKFLOW_FILE}.bak"
fi

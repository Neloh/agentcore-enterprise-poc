#!/usr/bin/env bash
# One-time setup: foundation stack (ECR, guardrail, IAM, SNS) and the CI/CD pipeline stack.
#
#   AWS_PROFILE=<profile> AWS_REGION=us-east-1 \
#   CONNECTION_ARN=arn:aws:codeconnections:... REPO=owner/repo BRANCH=main \
#   ALERT_EMAIL=you@example.com ./scripts/bootstrap.sh
set -euo pipefail

PROJECT="${PROJECT:-agentcore-poc}"
REGION="${AWS_REGION:-us-east-1}"
: "${CONNECTION_ARN:?set CONNECTION_ARN (CodeConnections ARN, status AVAILABLE)}"
: "${REPO:?set REPO=owner/repo}"
BRANCH="${BRANCH:-main}"

cd "$(dirname "$0")/.."

echo "Account: $(aws sts get-caller-identity --query Account --output text)  Region: $REGION"

aws cloudformation deploy --region "$REGION" --stack-name "$PROJECT-foundation" \
  --template-file infra/foundation.yaml --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides ProjectName="$PROJECT" AlertEmail="${ALERT_EMAIL:-}"

aws cloudformation deploy --region "$REGION" --stack-name "$PROJECT-pipeline" \
  --template-file infra/pipeline.yaml --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides ProjectName="$PROJECT" ConnectionArn="$CONNECTION_ARN" \
    FullRepositoryId="$REPO" BranchName="$BRANCH"

aws cloudformation describe-stacks --region "$REGION" --stack-name "$PROJECT-pipeline" \
  --query 'Stacks[0].Outputs' --output table

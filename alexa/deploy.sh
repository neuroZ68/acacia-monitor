#!/bin/sh
# Creates or updates the acacia-cue stack in us-east-1 and uploads the function code.
# Usage: ./alexa/deploy.sh [sensor-skill-id] [voice-skill-id] [clip-key]
# Arguments you leave out keep their current values, so a plain ./alexa/deploy.sh
# after a code change never removes Alexa's permission to call the function.
# Needs a current `aws login`. Review alexa/template.yaml before running.
set -eu
cd "$(dirname "$0")/.."
export AWS_REGION=us-east-1 AWS_PAGER=""
STACK=acacia-cue

overrides=""
[ -n "${1:-}" ] && overrides="$overrides SensorSkillId=$1"
[ -n "${2:-}" ] && overrides="$overrides VoiceSkillId=$2"
[ -n "${3:-}" ] && overrides="$overrides ClipKey=$3"

# Word splitting of $overrides is intended; skill IDs and clip keys contain no spaces.
# shellcheck disable=SC2086
aws cloudformation deploy \
    --stack-name "$STACK" \
    --template-file alexa/template.yaml \
    --capabilities CAPABILITY_IAM \
    --no-fail-on-empty-changeset \
    ${overrides:+--parameter-overrides $overrides}

function_name="$(aws cloudformation describe-stack-resource --stack-name "$STACK" \
    --logical-resource-id Function --query 'StackResourceDetail.PhysicalResourceId' --output text)"

package="$(mktemp -d)/lambda.zip"
zip -j -q "$package" alexa/lambda_function.py
aws lambda update-function-code --function-name "$function_name" \
    --zip-file "fileb://$package" --query 'LastUpdateStatus' --output text >/dev/null
aws lambda wait function-updated --function-name "$function_name"
rm -f "$package"

aws cloudformation describe-stacks --stack-name "$STACK" \
    --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output table

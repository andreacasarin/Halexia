# Deploying asynchronous answers

Deploy in an Alexa-supported Lambda region matching the skill endpoint (for example, `eu-west-1`). Use Python 3.12 and the existing Home Assistant account link. No Home Assistant or Ollama configuration changes are required. All resources incur normal AWS usage charges.

## Resources and permissions

`deployment.yaml` supports a new installation or adding a worker/table to an existing bridge. It creates:

- One pay-per-request DynamoDB table, encrypted at rest, with partition key `owner`, sort key `key`, and TTL attribute `expires_at`. No scans or indexes are needed by the application.
- One IAM-only worker Lambda, Python 3.12, 256 MB, 120-second execution timeout. It reads/updates only the jobs table. It does not have Alexa triggers, function URLs, or permission to invoke the bridge.
- Worker asynchronous invocation configuration: maximum event age 60 seconds and zero function-error retries. There is no DLQ or failure destination, because either would retain credential-bearing events. Conditional claims still defend against duplicate delivery.
- A worker log group with seven-day retention and a role limited to that log group and table.
- For a new installation: an eight-second/256 MB bridge Lambda, its log group/role, and both Alexa permissions restricted to `SkillId`.
- For an existing installation: only an additional inline policy on the existing bridge role, granting `GetItem`, `PutItem`, `UpdateItem` on this table and `InvokeFunction` on this worker. `PutItem` also authorizes the puts inside `TransactWriteItems`; there is no separate IAM action for that transaction. The existing function, role, logging permissions, endpoint ARN, and Alexa triggers are retained.

Deployment credentials need CloudFormation stack management and the resource creation/update permissions above, IAM role/policy management and `iam:PassRole` for Lambda roles, S3 artifact access, and (for an upgrade) Lambda configuration/code read/update plus `PublishVersion`. Runtime roles need no S3 access. Keep the artifact bucket private. Do not grant general Alexa users AWS IAM access.

## Settings

| Variable | Bridge | Worker | Purpose |
| --- | --- | --- | --- |
| `BASE_URL` | Required | Required | Plain trusted HTTPS Home Assistant origin, no path/query/fragment. |
| `AGENT_ID` | Required for Custom | Required | Exact conversation entity ID, e.g. `conversation.ollama_conversation`. |
| `JOBS_TABLE` | Required for Custom | Required | Table name from this stack. |
| `WORKER_FUNCTION_ARN` | Required for Custom | Unused | Unqualified worker ARN configured by the stack. |
| `SKILL_ID` | Required for Custom | Unused | Existing Alexa application ID. |
| `RESPONSE_BUDGET_SECONDS` | Optional, default/max `5` | Unused | Initial-question dispatch/poll budget; remaining invocation time minus one second can shorten it. Setting it too low can prevent submission. Status requests use the fixed bounded wait described below. |
| `DEBUG` | Optional | Optional | Any nonempty value enables sanitized failure warnings. |

Status requests poll the existing job for up to five seconds, within a 6.6-second total application budget that reserves 1.6 seconds for the final storage read and result update. Cold startup and remaining Lambda time minus one second may shorten this wait. Already finished, failed, unavailable, or expired jobs return immediately. If a job completes, fails, or expires during polling, that outcome is returned in the same follow-up. If it stays pending, the response explicitly asks the user to try again, sets `shouldEndSession=false`, and includes an Alexa reprompt. The reprompt is a fixed reminder on a listening timeout, not an automatic saved-answer delivery. On `SessionEndedRequest`, the handler returns no response speech and records only whitelisted reason/error codes. This adds no resources, IAM permissions, environment variables, or interaction-model changes.

Fixed application bounds: 110-second worker socket timeout, 180-second processing deadline from reservation, and 900-second result/deduplication retention. Keep the worker's configured timeout at 120 seconds and asynchronous maximum event age at 60 seconds. HTTP socket timeouts alone do not bound total inference duration; Lambda supplies the hard worker bound. The Smart Home HTTP timeout remains six seconds.

The worker requires outbound HTTPS to Home Assistant; both functions require regional DynamoDB access and the bridge requires the Lambda Invoke API. The template uses Lambda's normal public networking. If retaining a VPC-attached bridge, ensure its existing network can reach these AWS services through NAT or private endpoints.

## Build and upload

From the repository root, with an authenticated AWS CLI and the intended profile selected (use `--profile NAME` on AWS commands when needed):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s . -p 'test_*.py'
.venv/bin/cfn-lint deployment.yaml
python3 package.py

export HALEXIA_REGION=eu-west-1
export HALEXIA_STACK=halexia-async
export HALEXIA_BUCKET=halexia-deploy-YOUR_ACCOUNT_ID-eu-west-1
export HALEXIA_SKILL_ID=amzn1.ask.skill.YOUR_SKILL_ID
export HALEXIA_BASE_URL=https://your-home-assistant.example
export HALEXIA_AGENT_ID=conversation.ollama_conversation
export HALEXIA_CODE_KEY="halexia/$(shasum -a 256 dist/halexia.zip | cut -d ' ' -f1)/halexia.zip"
```

Use an existing private bucket in that region, or create one (omit the location constraint only for `us-east-1`):

```sh
aws s3api create-bucket --region "$HALEXIA_REGION" --bucket "$HALEXIA_BUCKET" \
  --create-bucket-configuration LocationConstraint="$HALEXIA_REGION"
aws s3api put-public-access-block --region "$HALEXIA_REGION" --bucket "$HALEXIA_BUCKET" \
  --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3 cp dist/halexia.zip "s3://$HALEXIA_BUCKET/$HALEXIA_CODE_KEY" \
  --region "$HALEXIA_REGION" --sse AES256
```

A content-derived object key is important: CloudFormation does not detect a new ZIP uploaded over an unchanged key. Rerun the build/upload and deployment steps on updates. The source archive, local tests, photos, and unrelated files are never uploaded to Lambda.

## Existing Lambda upgrade (retain the endpoint ARN)

Set these values for the existing function and skill you operate:

```sh
export HALEXIA_FUNCTION=arn:aws:lambda:eu-west-1:YOUR_ACCOUNT_ID:function:YOUR_EXISTING_FUNCTION
export HALEXIA_SKILL_ID=amzn1.ask.skill.YOUR_SKILL_ID
```

Read only the needed settings; do not dump the full environment or events:

```sh
export HALEXIA_BASE_URL="$(aws lambda get-function-configuration --region "$HALEXIA_REGION" \
  --function-name "$HALEXIA_FUNCTION" --query Environment.Variables.BASE_URL --output text)"
export HALEXIA_AGENT_ID="$(aws lambda get-function-configuration --region "$HALEXIA_REGION" \
  --function-name "$HALEXIA_FUNCTION" --query Environment.Variables.AGENT_ID --output text)"
export HALEXIA_BRIDGE_ROLE="$(aws lambda get-function-configuration --region "$HALEXIA_REGION" \
  --function-name "$HALEXIA_FUNCTION" --query Role --output text)"

aws cloudformation deploy --region "$HALEXIA_REGION" --stack-name "$HALEXIA_STACK" \
  --template-file deployment.yaml --capabilities CAPABILITY_IAM \
  --parameter-overrides CodeBucket="$HALEXIA_BUCKET" CodeKey="$HALEXIA_CODE_KEY" \
    BaseUrl="$HALEXIA_BASE_URL" AgentId="$HALEXIA_AGENT_ID" SkillId="$HALEXIA_SKILL_ID" \
    ExistingBridgeArn="$HALEXIA_FUNCTION" ExistingBridgeRoleName="${HALEXIA_BRIDGE_ROLE##*/}"
```

Wait for stack completion before upgrading the bridge. The following script preserves unrelated environment settings in memory, publishes a rollback version, adds the new settings first, then uploads the code. It supports both the original flat handler and the packaged handler without changing the existing handler or trigger policy. No tokens are printed or written to a configuration file.

```sh
.venv/bin/python - <<'PY'
import io
import os
from pathlib import Path
import zipfile
import boto3

region = os.environ['HALEXIA_REGION']
function = os.environ['HALEXIA_FUNCTION']
lam = boto3.client('lambda', region_name=region)
cfn = boto3.client('cloudformation', region_name=region)
config = lam.get_function_configuration(FunctionName=function)
if config['Handler'] not in ('lambda_function.lambda_handler', 'alexa_bridge.lambda_function.lambda_handler'):
    raise SystemExit('Unsupported handler; review its packaging before upgrading.')
if config['Runtime'] != 'python3.12':
    raise SystemExit('Use Python 3.12 before this upgrade.')
stack = cfn.describe_stacks(StackName=os.environ['HALEXIA_STACK'])['Stacks'][0]
outputs = {item['OutputKey']: item['OutputValue'] for item in stack['Outputs']}
if outputs['BridgeArn'] != config['FunctionArn']:
    raise SystemExit('Stack targets a different bridge.')
variables = dict(config.get('Environment', {}).get('Variables', {}))
variables.update(JOBS_TABLE=outputs['JobsTable'], WORKER_FUNCTION_ARN=outputs['WorkerArn'],
                 SKILL_ID=os.environ['HALEXIA_SKILL_ID'], RESPONSE_BUDGET_SECONDS='5')
# The account-link credential is supplied by Alexa, never a shared environment token.
variables.pop('HA_TOKEN', None)
archive = io.BytesIO()
prefix = '' if config['Handler'] == 'lambda_function.lambda_handler' else 'alexa_bridge/'
with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as package:
    for name in ('lambda_function.py', 'LICENSE'):
        package.writestr(prefix + name, Path(name).read_bytes())
rollback = lam.publish_version(FunctionName=function, Description='Before Halexia asynchronous answers')
print('Rollback version:', rollback['Version'])
lam.update_function_configuration(
    FunctionName=function, Environment={'Variables': variables}, Timeout=8,
    MemorySize=max(config['MemorySize'], 256),
    LoggingConfig={'LogFormat': 'JSON', 'ApplicationLogLevel': 'INFO', 'SystemLogLevel': 'INFO'},
)
lam.get_waiter('function_updated_v2').wait(FunctionName=function)
lam.update_function_code(FunctionName=function, ZipFile=archive.getvalue())
lam.get_waiter('function_updated_v2').wait(FunctionName=function)
print('Bridge upgraded:', config['FunctionArn'])
PY
```

This updates the existing unqualified function only. If an installation uses a version/alias endpoint, publish and move that alias after validation instead. Keep the same Home Assistant authorization URLs, scopes, account link, and both existing Alexa triggers. Do not point Alexa at the worker.

## New installation

Use the build/upload variables above and deploy without the two existing-bridge parameters:

```sh
aws cloudformation deploy --region "$HALEXIA_REGION" --stack-name "$HALEXIA_STACK" \
  --template-file deployment.yaml --capabilities CAPABILITY_IAM \
  --parameter-overrides CodeBucket="$HALEXIA_BUCKET" CodeKey="$HALEXIA_CODE_KEY" \
    BaseUrl="$HALEXIA_BASE_URL" AgentId="$HALEXIA_AGENT_ID" SkillId="$HALEXIA_SKILL_ID"
aws cloudformation describe-stacks --region "$HALEXIA_REGION" --stack-name "$HALEXIA_STACK" \
  --query 'Stacks[0].Outputs' --output table
```

Set both Alexa endpoints to `BridgeArn`. The stack creates both restricted triggers. Follow the account-linking instructions in [README.md](README.md); an existing Smart Home deployment keeps its `smart_home` scope and link.

## Interaction models

English (`en-GB`) is the bridge’s default locale when Alexa omits a locale, and English is the fallback for bridge prompts in unsupported languages. Italian requests (`it-IT`) receive the Italian prompts and reprompts. Both supported locales need their own built `AnswerStatusIntent` model. Changes to Lambda timing, acknowledgement text, reprompts, or diagnostics do not require reimporting/rebuilding an unchanged model. The latest pending-response update already includes both English and Italian text; import/build the English model once if that locale has not yet received `AnswerStatusIntent`.

In the Alexa Developer Console, select the existing skill, then each Custom locale (`it-IT`, `en-GB`), open **Interaction Model → JSON Editor**, import the matching file from `interaction_models/`, save, and build. Wait for a successful build in each locale. Review any manually customized utterances before replacing them. `AnswerStatusIntent` must include the Italian utterances `è pronta`, `hai la risposta`, and `dimmi la risposta`; no question marks are needed in model samples. Verify the build for each selected locale: click **Evaluate Model / Valuta il modello → Utterance Profiler** and check that `dimmi la risposta` resolves to `AnswerStatusIntent` for `it-IT` (and `tell me the answer` for `en-GB`). If the profiler says no model is built, importing/saving is not sufficient: run **Build Model / Build Skill** and wait for success. In the Italian console this action is labeled **Crea skill**, the blue button at the far upper right beside **Last successful build**; it compiles the currently open skill model. Keep the existing Custom and Smart Home endpoints on the bridge ARN. Enable **Development** testing for the skill and reload the Test page after the build.

## Verify timing independently of inference

1. Run the local suite and template lint above. `test_frontend_returns_while_independent_worker_is_still_running_wall_clock` blocks a simulated independent worker and asserts that the bridge returns pending in under 5.5 seconds while inference remains active. This checks separation, not AWS or Alexa latency.
2. In AWS, inspect the worker asynchronous configuration: `aws lambda get-function-event-invoke-config --region "$HALEXIA_REGION" --function-name WORKER_ARN`. Confirm 60-second maximum age, zero retries, and no destinations. Confirm its timeout is 120 seconds. Confirm the existing bridge still has both skill-restricted triggers with `get-policy`.
3. Test with a linked Alexa account through the Alexa simulator and then an Echo. Do not paste real linked tokens into CLI test events, files, or logs. Correlate `custom_job` lines using `job` and hashed `request`. The bridge's `FRONTEND_RETURN elapsed_ms` measures handler work independently of the worker's terminal elapsed time. Check Lambda `REPORT`/cold-start duration as well: handler elapsed time excludes platform startup and Alexa transport. Alexa's own request/response timing must remain comfortably below eight seconds.
4. For a slow/cold request, confirm `FRONTEND_RETURN` precedes the worker's terminal line. After a follow-up, confirm the returned speech comes from the same job and there was exactly one `RUNNING` claim. Use a naturally cold/slow inference request; do not change Home Assistant or the inference stack just to force latency.
5. Check CloudWatch metrics `AsyncEventsDropped`, worker errors/timeouts/throttles, and bridge duration. Queue loss or a hard worker timeout must become a spoken failure after 180 seconds, without any automatic inference retry. Do not configure raw-event failure destinations to investigate.

## Real-Echo acceptance

Use read-only questions and record the result and separate acknowledgement/inference durations. Alexa must perform recognition and speech synthesis throughout.

| Check | Steps and expected result |
| --- | --- |
| Fast | Open the skill; ask a warmed, quick question. Alexa speaks the actual answer directly. |
| Slow | Ask after the model has naturally gone idle/cold. Alexa acknowledges within its response window. After a few seconds, say `è pronta?`; Alexa speaks the saved answer. It must not speak automatically before the follow-up. |
| Still pending / completion during wait | Ask `hai la risposta?` before completion. The handler waits up to five seconds for the existing job: return its answer if it finishes during that window, otherwise say it is still preparing. Confirm that no additional inference is submitted. |
| New invocation | Let the session close, reopen the skill, and say `dimmi la risposta`. The same retained answer is available. |
| Conversation | Retrieve an answer, then ask a related follow-up. Confirm Home Assistant's conversation context continues without exposing conversation contents in shared logs. |
| Multiple questions | Submit a second question before retrieving the first answer. Alexa explicitly rejects it; it is not processed later. |
| Repeated result | Request the result twice. Both requests replay it until a new question replaces it or its 15-minute retention expires. |
| Failure | During a naturally occurring/unavailable-backend failure, confirm explicit failure or account-link/permission prompt; no invented answer or silent resubmission. Local tests inject 401/403, timeouts, malformed replies, worker faults, and lost dispatch without altering Home Assistant. |
| Expiry | Wait over 15 minutes without replacing a job, reopen, and ask for the result. Alexa says it is unavailable/expired. |
| Isolation | A different linked Alexa user cannot retrieve the original user's answer. A relink/credential refresh also cannot retrieve that prior credential's result. |
| Smart Home | Perform the existing read-only discovery/status check and confirm it takes the original route; physical control tests require the operator's normal device precautions. |

Record this checklist's actual results in the handoff. Local or simulator tests cannot establish that a real Echo's ASR selected the correct intent or that its listening window was sufficient.

## Rollback and cleanup

For an existing bridge, use the printed rollback version to obtain its prior code with `get_function(FunctionName=..., Qualifier=VERSION)` and its prior configuration with `get_function_configuration(..., Qualifier=VERSION)`. Restore that ZIP and its original handler/environment/timeout/memory/logging settings to the unqualified function with the same SDK update/wait sequence above; retrieve the download URL and configuration only in memory and never print the environment. No account relink or Alexa endpoint change is necessary. Restore the old model JSON from git if needed.

After the bridge no longer uses asynchronous jobs and no worker is running, deleting the add-on stack removes its worker, table, log group, and added inline policy; it leaves the pre-existing bridge, role, and triggers. Deleting the table permanently removes retained answers. A new-install stack also owns its bridge/triggers. Artifact buckets and published rollback versions are outside the stack; remove them separately when no longer needed. Do not switch an existing stack between the new-install and existing-bridge modes.

## Validation record — 5 November 2026

- **45 local tests passed** covering immediate and delayed results, five-second status waits, consecutive pending acknowledgements and reprompts, failures, expiry, user/credential isolation, duplicate delivery, reservation conflicts, conversation continuity, English defaults, Italian responses, sanitized diagnostics, Smart Home forwarding, and reproducible archives. Deployment template lint and ZIP integrity checks passed.
- Live AWS checks verified independent dispatch, explicit Home Assistant authorization failure, replay, isolation, configured timeout/TTL policies, and preserved Alexa triggers without using real account-link credentials in test events. A cold bridge returned pending in 3.400 seconds of handler work (3.732 seconds client round trip), and its worker finished after the bridge returned.
- A linked Italian simulator conversation confirmed the complete slow-answer flow: pending acknowledgement, still-pending follow-up, then the saved answer. Logs correlated one job and one worker claim across all turns. Initial acknowledgement: 3.400 seconds of handler work; worker completion: 12.458 seconds of handler work; result retrieval: 0.017 seconds.
- Live AWS tests with isolated synthetic job state verified two consecutive pending response payloads, each with explicit speech, an open session, and a localized reprompt, after 5.346 and 5.080 seconds client round trip. Completion during a later follow-up returned its result in that same request after 4.560 seconds. Synthetic records were removed; these tests made no Home Assistant calls and started no inference.
- The operator confirmed the Italian flow working on a physical Echo after the pending-response update. This is operator-reported acceptance; Echo ASR/TTS timings were not measured separately.
- Manual English-device testing and the remaining deployment acceptance checks above are not independently confirmed. Application-level English/default-language behavior and the corresponding result-intent samples are covered by tests.

Keep installation-specific ARNs, table/bucket names, rollback-version numbers, and local troubleshooting records in your private operational notes. Record a rollback version before each upgrade; never include real credentials or private questions/answers in shared diagnostics.

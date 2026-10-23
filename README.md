# Halexia

Halexia provides one Alexa skill with Smart Home and Custom conversation models, backed by the same AWS Lambda. Smart Home directives are forwarded to Home Assistant's Smart Home API. Custom requests are sent to Home Assistant's REST conversation API and answered by the selected conversation agent. The bridge does not translate prompts, turn Custom requests into device commands, or provide an arbitrary Alexa fallback for Smart Home.

Home Assistant remains responsible for exposed entities, Smart Home controls, and the conversation agent's instructions and permissions. Alexa account linking supplies the user's Home Assistant access token for each request.

## Build and deploy

From the root of a standalone checkout, run:

```sh
python3 package.py
```

This creates `dist/halexia.zip` for Lambda and `dist/halexia-source.zip` for the source distribution. The runtime ZIP retains `alexa_bridge/lambda_function.py`; the handler is `alexa_bridge.lambda_function.lambda_handler`. The flat source ZIP contains only `.gitignore`, `LICENSE`, `NOTICE`, `README.md`, both locale models, `lambda_function.py`, `package.py`, `test_bridge.py`, and `third_party/LICENSE`. The package builder does not include files outside this project folder.

In AWS Lambda, choose Python 3.12 and set the handler to `alexa_bridge.lambda_function.lambda_handler`. Upload `dist/halexia.zip`. The bridge uses only Python's standard library. In **Configuration → General configuration → Edit**, set **Timeout** to **8 seconds**. AWS Lambda's default is 3 seconds; the bridge reserves about one second before its HTTP call, leaving roughly 2 seconds for Home Assistant at that default. The bridge's HTTP blocking timeout is up to 6 seconds, but this is not a strict end-to-end deadline. Alexa expects a response within its own response window, so use the 8-second Lambda setting and measure complete requests.

Alternatively, paste the unchanged contents of `lambda_function.py` from the flat standalone checkout into the Lambda code editor as `lambda_function.py` and use the handler `lambda_function.lambda_handler`. Save and deploy the function after changing its code or environment.

Set these Lambda environment variables:

```text
BASE_URL=https://your-home-assistant.example
AGENT_ID=conversation.your_agent
```

`BASE_URL` is the HTTPS Home Assistant base URL without a path, query, or fragment. It is shared by both models. Alexa and Lambda need trusted HTTPS access to this host on port 443. `AGENT_ID` is used only for Custom conversation and must be the conversation entity ID for the Home Assistant agent to use.

The bridge always sends `AGENT_ID`. Home Assistant's REST `conversation.process` API defaults to its built-in agent only when `agent_id` is omitted; it does not automatically select the preferred Assist pipeline. Running a preferred Assist pipeline uses Home Assistant's WebSocket conversation flow, which this bridge does not implement.

Optional troubleshooting setting:

```text
DEBUG=1
```

Any non-empty `DEBUG` value enables sanitized Custom failure warnings. Remove the variable or leave it empty to disable them. Do not set `DEBUG=0`; it is non-empty and therefore enables diagnostics. The warnings contain only fixed categories and numeric HTTP status or configuration key names. They never include request or response contents, URLs, headers, questions, speech, tokens, or exception details.

## Configure the Alexa skill

Use one Alexa Smart Home skill. Configure the Smart Home model for both `it-IT` and `en-GB`, then add a Custom model for each locale under **MODELS**. Import the matching files from `interaction_models/`, save them, and build both Custom interaction models. Configure the same Lambda ARN separately for the Smart Home and Custom endpoints. Enable both the **Alexa Smart Home** and **Alexa Skills Kit** Lambda triggers, each restricted to the same skill ID.

For an existing Smart Home deployment, keep its Home Assistant configuration, OAuth account link, and `smart_home` scope. Keep the existing `BASE_URL`, add `AGENT_ID` for Custom conversation, configure the Custom endpoint with the same Lambda ARN, and add the Alexa Skills Kit trigger restricted to that skill ID. Do not change the existing Smart Home account link to the Custom `intent` scope, and do not relink unless the link is invalid or its settings actually changed. Remove any old shared `HA_TOKEN` Lambda variable; the bridge uses the token supplied through Alexa account linking.

For a new Custom model that needs account linking, configure **Auth Code Grant** with:

- Authorization URI: `https://your-home-assistant.example/auth/authorize`
- Access Token URI: `https://your-home-assistant.example/auth/token`
- Client ID: `https://layla.amazon.com/` for EU, `https://pitangui.amazon.com/` for US, or `https://alexa.amazon.co.jp/` for Japan/Australia. Keep the trailing slash and choose the value for the Alexa region used by the skill.
- Client Secret: any non-empty value; Home Assistant ignores it.
- Client authentication: credentials in request body, not HTTP Basic.
- Scope: `intent`.
- Leave the domain list and default access-token expiration blank. Turn off **Allow users to enable skill without account linking**.

Keep any existing Smart Home account-link configuration and scope unchanged when adding Custom conversation. The `intent` scope is shown for a new Custom setup; Home Assistant currently does not enforce it for Custom conversation. When the console asks about PKCE, follow compatibility instructions for the installed Home Assistant version. The authorization and token URLs must use the same trusted HTTPS Home Assistant host.

Halexia is the project name, not an Alexa invocation phrase. The Custom model uses explicit locale models. Its Italian invocation remains `assistente casa` and its British English invocation remains `home assistant`. Use a carrier phrase for the request and follow-ups, such as `chiedi …` or `domanda …` in Italian, and `ask …` or `question …` in English. Smart Home uses Alexa's built-in invocation and controls.

## Operation and troubleshooting

Smart Home directives are sent unchanged to `<BASE_URL>/api/alexa/smart_home`. Their bearer token is read from Alexa's `endpoint.scope`, `payload.grantee`, or `payload.scope` field when present. Custom requests go to `<BASE_URL>/api/conversation/process` with the Alexa locale, `AGENT_ID`, and conversation context. The Home Assistant Smart Home integration and conversation agent are separate controls.

The spoken Custom connection error is generic and does not identify the cause; it may be returned before the HTTP timeout. To investigate, set `DEBUG=1` in Lambda environment variables, save the setting, deploy the current package, and ask one read-only question such as `chiedi che ora è`. Check the latest invocation in **CloudWatch → Log groups → `/aws/lambda/<function-name>`** for a `custom_failure` line.

- `custom_failure category=missing_config config=BASE_URL` or `custom_failure category=missing_config config=AGENT_ID`: add the named variable.
- `custom_failure category=invalid_base_url config=BASE_URL`: use a plain HTTPS base URL with no path, query, or fragment.
- `custom_failure category=http_error status=401`: relink the Alexa account. `status=403` means Home Assistant denied permission.
- `custom_failure category=http_error status=400`: Home Assistant rejected the request. Check the selected `AGENT_ID` and the Home Assistant side; this status alone does not identify the cause.
- `custom_failure category=timeout`, `tls_error`, or `connection_error`: compare the invocation duration with the timeout, and check Home Assistant availability, certificate validity, and Lambda network access.
- `custom_failure category=invalid_json`: the Home Assistant response was not valid JSON.
- `custom_failure category=unusable_speech`: the response did not contain usable plain speech; check the selected conversation agent and Home Assistant response.

The bridge does not retry. A timeout or connection failure may occur after Home Assistant received a request, so check Home Assistant before repeating a command that may have changed device state. Remove or unset `DEBUG` after troubleshooting. Share only the sanitized `custom_failure` line when asking for help; never enable full-event logging.

## Check locally

From the root of the standalone checkout, run the tests:

```sh
python3 -m unittest discover -s . -p 'test_*.py'
```

To build only the Lambda ZIP, run `python3 package.py --runtime-only`. A single Italian read-only conversation round trip was manually confirmed in the current setup. This does not validate other locales, Smart Home actions, Home Assistant outage behavior, or the house's physical controls.

## Standalone project and license

For a future public GitHub repository, create a new empty folder named `halexia` and extract only `dist/halexia-source.zip` into it. Create the repository from that extracted folder; do not copy the development folder or its Git history. The source ZIP is the package builder's allowlisted set of files. This preparation does not publish the repository, Alexa Store skill, or a hosted multi-house service.

The project is licensed under Apache License 2.0 as stated in [LICENSE](LICENSE). The adapted Smart Home flow retains Jason Hu's 2019 attribution and Apache 2.0 terms in [NOTICE](NOTICE) and [third_party/LICENSE](third_party/LICENSE).

## References

- [Home Assistant REST conversation API: send a sentence](https://contributors.home-assistant.io/core/internals/conversation-api/#send-a-sentence)
- [Home Assistant Assist pipelines: run a pipeline](https://contributors.home-assistant.io/core/internals/assist-pipelines/#run-a-pipeline)
- [Home Assistant Alexa intent account linking](https://www.home-assistant.io/integrations/alexa.intent/#account-linking)
- [Alexa Smart Home MCS developer console](https://developer.amazon.com/en-US/docs/alexa/smarthome/mcs-dev-console.html)
- [Alexa Smart Home MCS FAQ](https://developer.amazon.com/en-GB/docs/alexa/smarthome/mcs-faq.html)
- [Alexa account linking for Custom skills](https://developer.amazon.com/en-US/docs/alexa/account-linking/add-account-linking-logic-custom-skill.html)

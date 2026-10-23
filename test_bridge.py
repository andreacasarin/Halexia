import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import lambda_function as bridge
import package as package_builder


def ask_event(text="Turn on the hallway lights", locale="en-GB", attrs=None, new=False):
    return {
        "context": {"System": {"user": {"accessToken": "request-token"}}},
        "session": {"new": new, "attributes": attrs or {}},
        "request": {
            "type": "IntentRequest",
            "locale": locale,
            "intent": {"name": "AskHomeIntent", "slots": {"query": {"value": text}}},
        },
    }


def smart_home_event(name="Discover", token="smart-token", namespace="Alexa.Discovery", endpoint=True):
    directive = {
        "header": {
            "namespace": namespace,
            "name": name,
            "messageId": "message-1",
            "payloadVersion": "3",
            "correlationToken": "correlation-1",
        },
        "payload": {"scope": {"type": "BearerToken", "token": "payload-token"}},
    }
    if endpoint:
        directive["endpoint"] = {
            "endpointId": "device-1",
            "scope": {"type": "BearerToken", "token": token},
        }
    return {"directive": directive}


class Context:
    def get_remaining_time_in_millis(self):
        return 4200


class FakeHTTPResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self):
        return self.body


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "BASE_URL": "https://ha.example",
            "AGENT_ID": "conversation.test_agent",
            "HA_URL": "https://legacy.invalid",
            "HA_AGENT_ID": "conversation.legacy",
            "HA_TOKEN": "legacy-token-must-not-be-used",
            "DEBUG": "1",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_forwards_exact_text_locale_agent_and_existing_context(self):
        opener = unittest.mock.Mock()
        opener.open.return_value = FakeHTTPResponse(json.dumps({
            "response": {"speech": {"plain": {"speech": "Done"}}},
            "conversation_id": "conv-2",
        }).encode())
        with patch.object(bridge.urllib.request, "build_opener", return_value=opener):
            result = bridge.lambda_handler(ask_event("  tell me exactly  ", "it-IT", {"conversation_id": "conv-1"}), Context())
        request, timeout = opener.open.call_args.args[0], opener.open.call_args.kwargs["timeout"]
        self.assertEqual(request.full_url, "https://ha.example/api/conversation/process")
        self.assertEqual(request.get_header("Authorization"), "Bearer request-token")
        self.assertEqual(json.loads(request.data), {
            "text": "  tell me exactly  ", "language": "it-IT", "agent_id": "conversation.test_agent",
            "conversation_id": "conv-1",
        })
        self.assertEqual(timeout, 3.2)
        self.assertEqual(result["response"]["outputSpeech"], {"type": "PlainText", "text": "Done"})
        self.assertEqual(result["sessionAttributes"], {"conversation_id": "conv-2"})
        self.assertNotIn("request-token", json.dumps(result))

    def test_access_token_is_per_invocation_and_legacy_environment_is_ignored(self):
        opener = unittest.mock.Mock()
        opener.open.side_effect = [
            FakeHTTPResponse(b'{"response":{"speech":{"plain":{"speech":"One"}}}}'),
            FakeHTTPResponse(b'{"response":{"speech":{"plain":{"speech":"Two"}}}}'),
        ]
        with patch.object(bridge.urllib.request, "build_opener", return_value=opener):
            for token in ("user-one", "user-two"):
                event = ask_event()
                event["context"]["System"]["user"]["accessToken"] = token
                bridge.lambda_handler(event, Context())
        self.assertEqual(
            [call.args[0].get_header("Authorization") for call in opener.open.call_args_list],
            ["Bearer user-one", "Bearer user-two"],
        )

    def test_new_session_does_not_forward_old_context(self):
        opener = unittest.mock.Mock()
        opener.open.return_value = FakeHTTPResponse(b'{"response":{"speech":{"plain":{"speech":"Hi"}}}}')
        with patch.object(bridge.urllib.request, "build_opener", return_value=opener):
            bridge.lambda_handler(ask_event(attrs={"conversation_id": "old"}, new=True), Context())
        self.assertNotIn("conversation_id", json.loads(opener.open.call_args.args[0].data))

    def test_new_session_does_not_return_old_context_on_failure(self):
        opener = unittest.mock.Mock()
        opener.open.side_effect = TimeoutError()
        with patch.object(bridge.urllib.request, "build_opener", return_value=opener):
            result = bridge.lambda_handler(ask_event(attrs={"conversation_id": "old"}, new=True), Context())
        self.assertNotIn("sessionAttributes", result)

    def test_missing_slot_makes_no_request(self):
        event = ask_event(None)
        event["request"]["intent"]["slots"] = {}
        with patch.object(bridge.urllib.request, "build_opener") as opener:
            result = bridge.lambda_handler(event, Context())
        opener.assert_not_called()
        self.assertIn("didn't get", result["response"]["outputSpeech"]["text"])

    def test_missing_or_malformed_access_token_requests_link_without_network(self):
        for token in (None, "", "  ", "abc def", "abc\nxyz", "café", 42, [], {}):
            event = ask_event(attrs={"conversation_id": "old"})
            if token is None:
                del event["context"]["System"]["user"]["accessToken"]
            else:
                event["context"]["System"]["user"]["accessToken"] = token
            with self.subTest(token=token), patch.object(bridge.urllib.request, "build_opener") as opener:
                result = bridge.lambda_handler(event, Context())
            opener.assert_not_called()
            self.assertEqual(result["response"]["card"], {"type": "LinkAccount"})
            self.assertNotIn("sessionAttributes", result)
            self.assertNotIn("old", json.dumps(result))

        for alexa_context in (None, [], {"System": None}, {"System": {"user": []}}):
            event = ask_event()
            event["context"] = alexa_context
            with self.subTest(context=alexa_context), patch.object(bridge.urllib.request, "build_opener") as opener:
                result = bridge.lambda_handler(event, Context())
            opener.assert_not_called()
            self.assertEqual(result["response"]["card"], {"type": "LinkAccount"})

    def test_unauthorized_requests_relink_once_and_forbidden_is_distinct(self):
        for status, expect_card in ((401, True), (403, False)):
            with self.subTest(status=status):
                opener = unittest.mock.Mock()
                opener.open.side_effect = HTTPError("https://ha.example", status, "denied", {}, None)
                with patch.object(bridge.urllib.request, "build_opener", return_value=opener):
                    result = bridge.lambda_handler(ask_event(attrs={"conversation_id": "old"}), Context())
                opener.open.assert_called_once()
                self.assertEqual("card" in result["response"], expect_card)
                self.assertNotIn("sessionAttributes", result)
                self.assertNotIn("request-token", json.dumps(result))
                if expect_card:
                    self.assertEqual(result["response"]["card"], {"type": "LinkAccount"})
                else:
                    self.assertIn("not allowed", result["response"]["outputSpeech"]["text"])

    def test_custom_logs_missing_and_invalid_configuration_without_network(self):
        for env, expected in (
            ({"AGENT_ID": "conversation.test_agent"}, "category=missing_config config=BASE_URL"),
            ({"BASE_URL": "https://ha.example"}, "category=missing_config config=AGENT_ID"),
            ({"BASE_URL": "http://private-host", "AGENT_ID": "conversation.test_agent"}, "category=invalid_base_url config=BASE_URL"),
        ):
            with self.subTest(expected=expected), patch.dict(os.environ, {**env, "DEBUG": "1"}, clear=True), patch.object(bridge.urllib.request, "build_opener") as opener:
                with self.assertLogs(bridge.LOGGER, level="WARNING") as captured:
                    bridge.lambda_handler(ask_event(), Context())
            opener.assert_not_called()
            self.assertEqual(len(captured.output), 1)
            self.assertIn(expected, captured.output[0])

    def test_custom_logs_http_status_without_request_or_exception_secrets(self):
        event = ask_event("private-question", attrs={"conversation_id": "private-conversation"})
        event["context"]["System"]["user"]["accessToken"] = "private-access-token"
        opener = unittest.mock.Mock()
        opener.open.side_effect = HTTPError(
            "https://private-host/private-url-token", 400, "private-exception-message",
            {"X-Private": "private-header-value"}, None,
        )
        with patch.object(bridge.urllib.request, "build_opener", return_value=opener):
            with self.assertLogs(bridge.LOGGER, level="WARNING") as captured:
                result = bridge.lambda_handler(event, Context())
        opener.open.assert_called_once()
        self.assertIn("category=http_error status=400", captured.output[0])
        for private_value in (
            "private-question", "private-conversation", "private-access-token", "private-host",
            "private-url-token", "private-exception-message", "private-header-value",
        ):
            self.assertNotIn(private_value, captured.output[0])
        self.assertIn("can't confirm whether the action was carried out", result["response"]["outputSpeech"]["text"])

    def test_debug_env_gates_sanitized_logs_using_nonempty_string_semantics(self):
        results = []
        for debug_value in (None, ""):
            opener = unittest.mock.Mock()
            opener.open.side_effect = HTTPError("https://private-host", 400, "private failure", {}, None)
            with patch.dict(os.environ):
                if debug_value is None:
                    os.environ.pop("DEBUG", None)
                else:
                    os.environ["DEBUG"] = debug_value
                with patch.object(bridge.urllib.request, "build_opener", return_value=opener):
                    with patch.object(bridge.LOGGER, "warning") as warning:
                        results.append(bridge.lambda_handler(ask_event(), Context()))
            warning.assert_not_called()
            opener.open.assert_called_once()

        opener = unittest.mock.Mock()
        opener.open.side_effect = HTTPError("https://private-host", 400, "private failure", {}, None)
        with patch.dict(os.environ, {"DEBUG": "0"}), patch.object(bridge.urllib.request, "build_opener", return_value=opener):
            with self.assertLogs(bridge.LOGGER, level="WARNING") as captured:
                enabled_result = bridge.lambda_handler(ask_event(), Context())
        self.assertEqual(len(captured.output), 1)
        self.assertIn("category=http_error status=400", captured.output[0])
        self.assertEqual(results[0]["response"], results[1]["response"])
        self.assertEqual(results[0]["response"], enabled_result["response"])
        opener.open.assert_called_once()

    def test_custom_logs_timeout_connection_tls_json_and_speech_categories(self):
        failures = (
            (TimeoutError("private timeout"), None, "category=timeout"),
            (URLError("private connection detail"), None, "category=connection_error"),
            (ssl.SSLError("private certificate detail"), None, "category=tls_error"),
            (None, b"private malformed body", "category=invalid_json"),
            (None, b"\xff", "category=invalid_json"),
            (None, b'{"response":{}}', "category=unusable_speech"),
        )
        for failure, body, expected in failures:
            with self.subTest(expected=expected):
                opener = unittest.mock.Mock()
                if failure is not None:
                    opener.open.side_effect = failure
                else:
                    opener.open.return_value = FakeHTTPResponse(body)
                with patch.object(bridge.urllib.request, "build_opener", return_value=opener):
                    with self.assertLogs(bridge.LOGGER, level="WARNING") as captured:
                        bridge.lambda_handler(ask_event("private question"), Context())
                opener.open.assert_called_once()
                self.assertEqual(len(captured.output), 1)
                self.assertIn(expected, captured.output[0])
                self.assertNotIn("private", captured.output[0])

    def test_transport_http_malformed_empty_and_timeout_are_honest_and_not_retried(self):
        failures = [
            URLError("offline"),
            HTTPError("https://ha.example", 302, "redirect", {}, None),
            b"not json",
            b'{"response":{}}',
            b'[]',
            b'{"response":null}',
            b'{"response":{"speech":null}}',
            TimeoutError(),
        ]
        for failure in failures:
            with self.subTest(failure=failure):
                opener = unittest.mock.Mock()
                if isinstance(failure, bytes):
                    opener.open.return_value = FakeHTTPResponse(failure)
                else:
                    opener.open.side_effect = failure
                with patch.object(bridge.urllib.request, "build_opener", return_value=opener):
                    result = bridge.lambda_handler(ask_event(), Context())
                opener.open.assert_called_once()
                self.assertIn("can't confirm whether the action was carried out", result["response"]["outputSpeech"]["text"])

    def test_local_replies_preserve_session_context_for_follow_up(self):
        for intent in (
            {"name": "AMAZON.HelpIntent"},
            {"name": "AMAZON.FallbackIntent"},
            {"name": "AskHomeIntent", "slots": {}},
        ):
            event = {"session": {"attributes": {"conversation_id": "active"}}, "request": {"type": "IntentRequest", "locale": "en-GB", "intent": intent}}
            reply = bridge.lambda_handler(event, Context())
            self.assertEqual(reply["sessionAttributes"], {"conversation_id": "active"})

        opener = unittest.mock.Mock()
        opener.open.return_value = FakeHTTPResponse(b'{"response":{"speech":{"plain":{"speech":"Hello again"}}}}')
        with patch.object(bridge.urllib.request, "build_opener", return_value=opener):
            bridge.lambda_handler(ask_event(attrs={"conversation_id": "active"}), Context())
        self.assertEqual(json.loads(opener.open.call_args.args[0].data)["conversation_id"], "active")

    def test_fresh_session_local_reply_drops_old_context(self):
        event = {"session": {"new": True, "attributes": {"conversation_id": "old"}}, "request": {"type": "IntentRequest", "locale": "en-GB", "intent": {"name": "AMAZON.HelpIntent"}}}
        self.assertNotIn("sessionAttributes", bridge.lambda_handler(event, Context()))

    def test_https_url_validation_precedes_network(self):
        for url in ("http://ha.example", "https://ha.example/base", "https://user:pass@ha.example", "https://ha.example?token=x", "https://ha.example#x"):
            with self.subTest(url=url), patch.dict(os.environ, {"BASE_URL": url}), patch.object(bridge.urllib.request, "build_opener") as opener:
                result = bridge.lambda_handler(ask_event(), Context())
                opener.assert_not_called()
                self.assertIn("can't confirm", result["response"]["outputSpeech"]["text"])

    def test_redirect_handler_refuses_redirect(self):
        handler = bridge._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://other.example"))

    def test_local_intents_and_follow_up_session_behavior(self):
        launch = {"request": {"type": "LaunchRequest", "locale": "it-IT"}}
        self.assertFalse(bridge.lambda_handler(launch, None)["response"]["shouldEndSession"])
        stop = {"request": {"type": "IntentRequest", "locale": "it-IT", "intent": {"name": "AMAZON.StopIntent"}}}
        self.assertTrue(bridge.lambda_handler(stop, None)["response"]["shouldEndSession"])
        fallback = {"request": {"type": "IntentRequest", "locale": "en-GB", "intent": {"name": "AMAZON.FallbackIntent"}}}
        self.assertFalse(bridge.lambda_handler(fallback, None)["response"]["shouldEndSession"])

    def test_interaction_model_json_has_one_search_query_slot(self):
        for filename in ("interaction_models/it-IT.json", "interaction_models/en-GB.json"):
            with open(filename, encoding="utf-8") as model_file:
                model = json.load(model_file)["interactionModel"]["languageModel"]
            if filename.endswith("it-IT.json"):
                self.assertEqual(model["invocationName"], "assistente casa")
            ask = next(intent for intent in model["intents"] if intent["name"] == "AskHomeIntent")
            self.assertEqual(ask["slots"], [{"name": "query", "type": "AMAZON.SearchQuery"}])
            self.assertTrue(ask["samples"])
            for sample in ask["samples"]:
                self.assertIn("{query}", sample)
                self.assertTrue(sample.replace("{query}", "").strip())

    def test_smart_home_discovery_forwards_full_event_and_returns_response_unchanged(self):
        event = smart_home_event()
        response_body = {
            "event": {
                "header": {"namespace": "Alexa.Discovery", "name": "Discover.Response", "payloadVersion": "3", "correlationToken": "correlation-1"},
                "payload": {"endpoints": [], "context": {"properties": []}},
            }
        }
        opener = unittest.mock.Mock()
        opener.open.return_value = FakeHTTPResponse(json.dumps(response_body).encode())
        with patch.object(bridge.urllib.request, "build_opener", return_value=opener):
            result = bridge.lambda_handler(event, Context())
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://ha.example/api/alexa/smart_home")
        self.assertEqual(request.get_header("Authorization"), "Bearer smart-token")
        self.assertEqual(json.loads(request.data), event)
        self.assertEqual(result, response_body)

    def test_smart_home_does_not_require_custom_agent_id(self):
        event = smart_home_event()
        opener = unittest.mock.Mock()
        opener.open.return_value = FakeHTTPResponse(b'{"event":{}}')
        with patch.dict(os.environ, {"BASE_URL": "https://ha.example"}, clear=True):
            with patch.object(bridge.urllib.request, "build_opener", return_value=opener):
                bridge.lambda_handler(event, Context())
        self.assertEqual(opener.open.call_args.args[0].full_url, "https://ha.example/api/alexa/smart_home")

    def test_smart_home_accept_grant_forwards_grantee_token_and_response(self):
        event = smart_home_event("AcceptGrant", "unused", "Alexa.Authorization", endpoint=False)
        event["directive"]["payload"]["grantee"] = {"type": "BearerToken", "token": "grant-token"}
        event["directive"]["payload"].pop("scope")
        response_body = {"event": {"header": {"namespace": "Alexa.Authorization", "name": "AcceptGrant.Response", "payloadVersion": "3"}, "payload": {}}}
        opener = unittest.mock.Mock()
        opener.open.return_value = FakeHTTPResponse(json.dumps(response_body).encode())
        with patch.object(bridge.urllib.request, "build_opener", return_value=opener):
            result = bridge.lambda_handler(event, Context())
        self.assertEqual(opener.open.call_args.args[0].get_header("Authorization"), "Bearer grant-token")
        self.assertEqual(json.loads(opener.open.call_args.args[0].data), event)
        self.assertEqual(result, response_body)

    def test_smart_home_token_source_priority_and_malformed_scope_stops_fallback(self):
        event = smart_home_event()
        event["directive"]["payload"]["grantee"] = {"type": "BearerToken", "token": "grantee-token"}
        opener = unittest.mock.Mock()
        opener.open.side_effect = [
            FakeHTTPResponse(b'{"event":{}}'),
            FakeHTTPResponse(b'{"event":{}}'),
        ]
        with patch.object(bridge.urllib.request, "build_opener", return_value=opener):
            bridge.lambda_handler(event, Context())
            event["directive"]["endpoint"].pop("scope")
            bridge.lambda_handler(event, Context())
        self.assertEqual(
            [call.args[0].get_header("Authorization") for call in opener.open.call_args_list],
            ["Bearer smart-token", "Bearer grantee-token"],
        )

        event = smart_home_event(token="bad token")
        event["directive"]["payload"]["grantee"] = {"type": "BearerToken", "token": "fallback-token"}
        with patch.object(bridge.urllib.request, "build_opener") as no_network:
            result = bridge.lambda_handler(event, Context())
        no_network.assert_not_called()
        self.assertEqual(result["event"]["payload"]["type"], "INVALID_AUTHORIZATION_CREDENTIAL")
        self.assertNotIn("sessionAttributes", result)
        self.assertNotIn("fallback-token", json.dumps(result))

    def test_smart_home_payload_scope_fallback_and_missing_credentials(self):
        event = smart_home_event(endpoint=False)
        opener = unittest.mock.Mock()
        opener.open.return_value = FakeHTTPResponse(b'{"event":{}}')
        with patch.object(bridge.urllib.request, "build_opener", return_value=opener):
            bridge.lambda_handler(event, Context())
        self.assertEqual(opener.open.call_args.args[0].get_header("Authorization"), "Bearer payload-token")

        event["directive"]["payload"].pop("scope")
        with patch.object(bridge.urllib.request, "build_opener") as no_network:
            result = bridge.lambda_handler(event, Context())
        no_network.assert_not_called()
        self.assertEqual(result["event"]["payload"]["type"], "EXPIRED_AUTHORIZATION_CREDENTIAL")
        self.assertNotIn("payload-token", json.dumps(result))
        self.assertNotIn("BearerToken", json.dumps(result))
        self.assertNotIn('"scope"', json.dumps(result))

    def test_smart_home_errors_have_safe_v3_headers_and_map_failures(self):
        cases = (
            (HTTPError("https://ha.example", 401, "denied", {}, None), "INVALID_AUTHORIZATION_CREDENTIAL", True),
            (HTTPError("https://ha.example", 403, "denied", {}, None), "INSUFFICIENT_PERMISSIONS", True),
            (HTTPError("https://ha.example", 503, "offline", {}, None), "ENDPOINT_UNREACHABLE", True),
            (TimeoutError(), "ENDPOINT_UNREACHABLE", True),
        )
        for failure, error_type, with_endpoint in cases:
            with self.subTest(failure=failure):
                event = smart_home_event()
                opener = unittest.mock.Mock()
                opener.open.side_effect = failure
                with patch.object(bridge.urllib.request, "build_opener", return_value=opener):
                    result = bridge.lambda_handler(event, Context())
                opener.open.assert_called_once()
                response_event = result["event"]
                self.assertEqual(response_event["header"]["namespace"], "Alexa")
                self.assertEqual(response_event["header"]["name"], "ErrorResponse")
                self.assertEqual(response_event["header"]["payloadVersion"], "3")
                self.assertEqual(response_event["header"]["correlationToken"], "correlation-1")
                self.assertEqual(response_event["payload"]["type"], error_type)
                self.assertEqual("endpoint" in response_event, with_endpoint)
                self.assertNotIn("smart-token", json.dumps(result))
                self.assertNotIn("BearerToken", json.dumps(result))

        event = smart_home_event(endpoint=False)
        opener = unittest.mock.Mock()
        opener.open.side_effect = TimeoutError()
        with patch.object(bridge.urllib.request, "build_opener", return_value=opener):
            result = bridge.lambda_handler(event, Context())
        self.assertEqual(result["event"]["payload"]["type"], "INTERNAL_ERROR")

    def test_malformed_directive_and_accept_grant_failures_are_valid_errors(self):
        with patch.object(bridge.urllib.request, "build_opener") as no_network:
            result = bridge.lambda_handler({"directive": {"header": {"namespace": "Alexa", "name": "Discover"}}}, Context())
        no_network.assert_not_called()
        self.assertEqual(result["event"]["payload"]["type"], "INVALID_DIRECTIVE")
        self.assertEqual(result["event"]["header"]["namespace"], "Alexa")
        self.assertEqual(result["event"]["header"]["payloadVersion"], "3")

        event = smart_home_event("AcceptGrant", "unused", "Alexa.Authorization", endpoint=False)
        event["directive"]["payload"]["grantee"] = {"type": "BearerToken", "token": "grant-token"}
        event["directive"]["payload"].pop("scope")
        opener = unittest.mock.Mock()
        opener.open.side_effect = HTTPError("https://ha.example", 503, "offline", {}, None)
        with patch.object(bridge.urllib.request, "build_opener", return_value=opener):
            result = bridge.lambda_handler(event, Context())
        self.assertEqual(result["event"]["header"]["namespace"], "Alexa.Authorization")
        self.assertEqual(result["event"]["payload"]["type"], "ACCEPT_GRANT_FAILED")
        self.assertNotIn("correlationToken", result["event"]["header"])
        self.assertNotIn("grant-token", json.dumps(result))

    def test_error_responses_drop_malformed_metadata_and_accept_grant_correlation(self):
        event = smart_home_event()
        event["directive"]["header"]["correlationToken"] = {"private": "metadata"}
        event["directive"]["endpoint"]["endpointId"] = 23
        opener = unittest.mock.Mock()
        opener.open.side_effect = TimeoutError()
        with patch.object(bridge.urllib.request, "build_opener", return_value=opener):
            result = bridge.lambda_handler(event, Context())
        self.assertNotIn("correlationToken", result["event"]["header"])
        self.assertNotIn("endpoint", result["event"])
        self.assertNotIn("private", json.dumps(result))

        event = smart_home_event("AcceptGrant", "unused", "Alexa.Authorization", endpoint=False)
        event["directive"]["header"]["correlationToken"] = "grant-correlation"
        event["directive"]["payload"]["grantee"] = {"type": "BearerToken", "token": "grant-token"}
        event["directive"]["payload"].pop("scope")
        opener = unittest.mock.Mock()
        opener.open.side_effect = TimeoutError()
        with patch.object(bridge.urllib.request, "build_opener", return_value=opener):
            result = bridge.lambda_handler(event, Context())
        self.assertEqual(result["event"]["payload"]["type"], "ACCEPT_GRANT_FAILED")
        self.assertNotIn("correlationToken", result["event"]["header"])
        self.assertNotIn("grant-correlation", json.dumps(result))

    def test_package_allowlists_are_explicit_and_archives_reproducible(self):
        self.assertEqual(package_builder.RUNTIME_FILES, ("lambda_function.py",))
        self.assertEqual(len(package_builder.SOURCE_FILES), 10)
        self.assertEqual(set(package_builder.SOURCE_FILES), {
            ".gitignore", "LICENSE", "NOTICE", "README.md",
            "interaction_models/en-GB.json", "interaction_models/it-IT.json",
            "lambda_function.py", "package.py", "test_bridge.py", "third_party/LICENSE",
        })
        for filename in package_builder.RUNTIME_FILES + package_builder.SOURCE_FILES:
            self.assertNotIn("..", Path(filename).parts)
            self.assertNotIn("__pycache__", Path(filename).parts)
        with tempfile.TemporaryDirectory() as temp_dir:
            first = Path(temp_dir) / "first.zip"
            second = Path(temp_dir) / "second.zip"
            package_builder._write_archive(first, package_builder.RUNTIME_FILES, prefix="alexa_bridge/")
            package_builder._write_archive(second, package_builder.RUNTIME_FILES, prefix="alexa_bridge/")
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                self.assertEqual(archive.namelist(), ["alexa_bridge/lambda_function.py"])

    def test_package_build_uses_its_own_folder_and_flat_source_entries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            copied = Path(temp_dir) / "alexa_bridge"
            copied.mkdir()
            for filename in package_builder.RUNTIME_FILES + package_builder.SOURCE_FILES:
                source = package_builder.ROOT / filename
                if source.is_file():
                    target = copied / filename
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(source.read_bytes())
            shutil.copyfile(package_builder.ROOT / "package.py", copied / "package.py")
            if not (copied / "LICENSE").exists():
                self.skipTest("canonical Apache LICENSE has not been added by the archivist")
            subprocess.run([sys.executable, "package.py"], cwd=copied, check=True)
            runtime = copied / "dist" / "halexia.zip"
            source = copied / "dist" / "halexia-source.zip"
            with zipfile.ZipFile(runtime) as archive:
                self.assertEqual(archive.namelist(), ["alexa_bridge/lambda_function.py"])
            with zipfile.ZipFile(source) as archive:
                self.assertEqual(archive.namelist(), sorted(package_builder.SOURCE_FILES))


if __name__ == "__main__":
    unittest.main()

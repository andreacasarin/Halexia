import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import lambda_function as bridge
import package as package_builder


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
        self.assertEqual(package_builder.RUNTIME_FILES, ("LICENSE", "lambda_function.py"))
        self.assertEqual(len(package_builder.SOURCE_FILES), 12)
        self.assertEqual(set(package_builder.SOURCE_FILES), {
            ".gitignore", "LICENSE", "README.md",
            "interaction_models/en-GB.json", "interaction_models/it-IT.json",
            "lambda_function.py", "package.py", "test_bridge.py",
            "test_async.py", "deployment.yaml", "DEPLOYMENT.md", "requirements-dev.txt",
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
                self.assertEqual(archive.namelist(), ["alexa_bridge/LICENSE", "alexa_bridge/lambda_function.py"])

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
                self.assertEqual(archive.namelist(), ["alexa_bridge/LICENSE", "alexa_bridge/lambda_function.py"])
            with zipfile.ZipFile(source) as archive:
                self.assertEqual(archive.namelist(), sorted(package_builder.SOURCE_FILES))


if __name__ == "__main__":
    unittest.main()

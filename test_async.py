"""Exercise the real DynamoDB request/condition API through Moto, without AWS access."""
import concurrent.futures
import copy
from datetime import datetime, timezone
import json
import os
import ssl
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError, URLError

import boto3
from moto import mock_aws
import lambda_function as bridge


ENV = {
    "BASE_URL": "https://ha.example", "AGENT_ID": "conversation.test_agent",
    "JOBS_TABLE": "jobs", "WORKER_FUNCTION_ARN": "worker", "SKILL_ID": "skill-1",
    "RESPONSE_BUDGET_SECONDS": "5", "DEBUG": "1", "AWS_DEFAULT_REGION": "eu-west-1",
}
ANSWER = {"conversation_id": "conversation-2", "response": {"speech": {"plain": {"speech": "In studio ci sono 22 gradi."}}}}


def event(name="AskHomeIntent", request_id="request-1", user="user-1", token="linked-token", new=False):
    return {
        "context": {"System": {"application": {"applicationId": "skill-1"},
                                "user": {"userId": user, "accessToken": token}}},
        "session": {"new": new, "attributes": {}},
        "request": {"type": "IntentRequest", "requestId": request_id, "locale": "it-IT",
                    "timestamp": datetime.fromtimestamp(bridge.time.time(), timezone.utc).isoformat(),
                    "intent": {"name": name, "slots": {"query": {"value": "Qual è la temperatura in studio?"}}}},
    }


def speech(reply):
    return reply["response"]["outputSpeech"]["text"]


class Clock:
    def __init__(self):
        self.elapsed = 0

    def time(self):
        return 1800000000 + self.elapsed

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.elapsed += seconds


class AsyncTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.aws = mock_aws()
        self.aws.start()
        self.db = boto3.client("dynamodb")
        self.db.create_table(TableName="jobs", BillingMode="PAY_PER_REQUEST",
                             AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"} for key in ("owner", "key")],
                             KeySchema=[{"AttributeName": "owner", "KeyType": "HASH"}, {"AttributeName": "key", "KeyType": "RANGE"}])
        self.queue = []
        self.immediate = False
        self.invoke = Mock(side_effect=self.dispatch)
        self.clients = patch.object(bridge, "_aws_client", side_effect=lambda service: self.db if service == "dynamodb" else SimpleNamespace(invoke=self.invoke))
        self.clients.start()
        self.clock = Clock()
        self.clock_patch = patch.object(bridge, "time", self.clock)
        self.clock_patch.start()
        self.infer = patch.object(bridge, "_ask_ha", return_value=copy.deepcopy(ANSWER))
        self.ask = self.infer.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.aws.stop)
        self.addCleanup(self.clients.stop)
        self.addCleanup(self.clock_patch.stop)
        self.addCleanup(self.infer.stop)

    def dispatch(self, **kwargs):
        self.assertEqual(kwargs["InvocationType"], "Event")
        payload = json.loads(kwargs["Payload"])
        self.queue.append(payload)
        if self.immediate:
            bridge.worker_handler(payload, None)
        return {"StatusCode": 202}

    def handle(self, **kwargs):
        return bridge.lambda_handler(event(**kwargs), None)

    def current(self):
        items = self.db.scan(TableName="jobs")["Items"]
        return next(bridge._decode(item) for item in items if item["key"]["S"] == "current")

    def finish(self):
        bridge.worker_handler(self.queue[-1], None)

    def test_immediate_result_and_duplicate_request_start_once(self):
        self.immediate = True
        first = self.handle()
        second = self.handle()
        self.assertEqual(speech(first), ANSWER["response"]["speech"]["plain"]["speech"])
        self.assertEqual(first, second)
        self.assertFalse(first["response"]["shouldEndSession"])
        self.assertEqual(first["sessionAttributes"], {"conversation_id": "conversation-2"})
        self.invoke.assert_called_once()
        self.ask.assert_called_once()
        self.assertTrue(self.current()["delivered"])

    def test_delayed_result_pending_follow_up_and_new_session_replay(self):
        self.assertIn("Sto preparando", speech(self.handle()))
        self.assertLessEqual(self.clock.elapsed, 5)
        self.assertIn("non è ancora pronta", speech(self.handle(name="AnswerStatusIntent", request_id="status-1")))
        self.ask.assert_not_called()
        self.finish()
        reply = self.handle(name="AnswerStatusIntent", request_id="status-2", new=True)
        self.assertEqual(speech(reply), ANSWER["response"]["speech"]["plain"]["speech"])
        self.assertEqual(reply, self.handle(name="AnswerStatusIntent", request_id="status-3", new=True))
        self.invoke.assert_called_once()

    def test_status_waits_for_completion_within_five_seconds_without_new_inference(self):
        self.handle()
        started = self.clock.elapsed
        sleep = self.clock.sleep
        finished = False
        def complete_during_wait(seconds):
            nonlocal finished
            sleep(seconds)
            if not finished and self.clock.elapsed - started >= 4.7:
                finished = True
                self.finish()
        with patch.object(self.clock, "sleep", side_effect=complete_during_wait):
            reply = self.handle(name="AnswerStatusIntent", request_id="status", new=True)
        self.assertEqual(speech(reply), ANSWER["response"]["speech"]["plain"]["speech"])
        self.assertGreaterEqual(self.clock.elapsed - started, 4.7)
        self.assertLessEqual(self.clock.elapsed - started, 5)
        self.assertEqual(reply["sessionAttributes"], {"conversation_id": "conversation-2"})
        self.invoke.assert_called_once()
        self.ask.assert_called_once()

    def test_status_wait_is_bounded_and_ready_or_missing_results_do_not_wait(self):
        self.handle()
        started = self.clock.elapsed
        pending = self.handle(name="AnswerStatusIntent", request_id="pending")
        self.assertIn("non è ancora pronta", speech(pending))
        self.assertFalse(pending["response"]["shouldEndSession"])
        self.assertIn("chiedermi di nuovo", pending["response"]["reprompt"]["outputSpeech"]["text"])
        self.assertAlmostEqual(self.clock.elapsed - started, 5)
        again = self.handle(name="AnswerStatusIntent", request_id="pending-again")
        self.assertEqual(again, pending)
        self.assertAlmostEqual(self.clock.elapsed - started, 10)
        self.ask.assert_not_called()
        self.finish()
        started = self.clock.elapsed
        self.handle(name="AnswerStatusIntent", request_id="ready")
        self.handle(name="AnswerStatusIntent", request_id="replay")
        self.assertIn("scaduta", speech(self.handle(name="AnswerStatusIntent", request_id="other", user="other")))
        self.assertEqual(self.clock.elapsed, started)
        self.invoke.assert_called_once()

    def test_status_wait_shortens_for_remaining_lambda_time(self):
        self.handle()
        started = self.clock.elapsed
        context = SimpleNamespace(get_remaining_time_in_millis=lambda: 4000)
        reply = bridge.lambda_handler(event(name="AnswerStatusIntent", request_id="status"), context)
        self.assertIn("non è ancora pronta", speech(reply))
        self.assertAlmostEqual(self.clock.elapsed - started, 1.4)
        self.invoke.assert_called_once()

    def test_initial_and_status_pending_replies_include_localized_reprompts(self):
        initial = self.handle()
        self.assertIn("chiedermi di nuovo", initial["response"]["reprompt"]["outputSpeech"]["text"])
        english = event(name="AnswerStatusIntent", request_id="status")
        english["request"]["locale"] = "en-GB"
        reply = bridge.lambda_handler(english, None)
        self.assertIn("isn't ready yet", speech(reply))
        self.assertIn("ask me again", reply["response"]["reprompt"]["outputSpeech"]["text"])
        self.assertFalse(reply["response"]["shouldEndSession"])
        self.invoke.assert_called_once()

    def test_missing_locale_defaults_questions_and_pending_status_to_english(self):
        question = event()
        question["request"].pop("locale")
        reply = bridge.lambda_handler(question, None)
        self.assertIn("I'm preparing the answer", speech(reply))
        self.assertEqual(self.queue[0]["locale"], "en-GB")
        status = event(name="AnswerStatusIntent", request_id="status")
        status["request"].pop("locale")
        reply = bridge.lambda_handler(status, None)
        self.assertIn("The answer isn't ready yet", speech(reply))
        self.assertIn("ask me again", reply["response"]["reprompt"]["outputSpeech"]["text"])
        self.finish()
        self.assertEqual(self.ask.call_args.args[1], "en-GB")
        self.invoke.assert_called_once()

    def test_status_budget_exhaustion_returns_pending_speech_and_reprompt(self):
        self.handle()
        get = bridge._Jobs.get
        reads = 0
        def budget_exhausted(store, owner, key="current"):
            nonlocal reads
            reads += 1
            if reads == 2:
                raise bridge._BudgetExpired()
            return get(store, owner, key)
        with patch.object(bridge._Jobs, "get", new=budget_exhausted):
            reply = self.handle(name="AnswerStatusIntent", request_id="status")
        self.assertIn("non è ancora pronta", speech(reply))
        self.assertFalse(reply["response"]["shouldEndSession"])
        self.assertIn("reprompt", reply["response"])
        self.invoke.assert_called_once()
        self.ask.assert_not_called()

    def test_session_end_returns_no_speech_and_logs_only_safe_reason(self):
        ended = event()
        ended["request"].update(type="SessionEndedRequest", reason="ERROR",
                                error={"type": "ENDPOINT_TIMEOUT", "message": "private-token"})
        with self.assertLogs(bridge.LOGGER, level="INFO") as logs:
            reply = bridge.lambda_handler(ended, None)
        self.assertIsNone(reply)
        output = "\n".join(logs.output)
        self.assertIn("reason=ERROR error=ENDPOINT_TIMEOUT", output)
        for private in ("private-token", "linked-token", "Qual è la temperatura", "user-1"):
            self.assertNotIn(private, output)
        self.invoke.assert_not_called()

    def test_routing_logs_whitelist_unknown_fields_and_hash_request_id(self):
        unknown = event(name="private-intent", request_id="private-request")
        unknown["request"].update(reason="private-reason", error={"type": "private-error"})
        with self.assertLogs(bridge.LOGGER, level="INFO") as logs:
            bridge.lambda_handler(unknown, None)
        output = "\n".join(logs.output)
        self.assertIn("type=IntentRequest intent=OTHER reason=NONE error=NONE", output)
        self.assertNotIn("private", output)
        self.invoke.assert_not_called()

    def test_status_returns_worker_failure_during_wait(self):
        self.handle()
        sleep = self.clock.sleep
        self.ask.side_effect = TimeoutError()
        def fail_during_wait(seconds):
            sleep(seconds)
            self.finish()
        with patch.object(self.clock, "sleep", side_effect=fail_during_wait):
            reply = self.handle(name="AnswerStatusIntent", request_id="status")
        self.assertIn("Non posso confermare", speech(reply))
        self.invoke.assert_called_once()
        self.ask.assert_called_once()

    def test_multiple_questions_are_rejected_and_rejected_delivery_never_runs_later(self):
        self.handle()
        self.assertIn("non è stata inviata", speech(self.handle(request_id="second")))
        self.finish()
        self.assertIn("precedente è pronta", speech(self.handle(request_id="third")))
        self.handle(name="AnswerStatusIntent", request_id="status")
        self.assertIn("non è stata inviata", speech(self.handle(request_id="second")))
        self.assertIn("non è stata inviata", speech(self.handle(request_id="third")))
        self.invoke.assert_called_once()
        self.handle(request_id="fourth")
        self.assertEqual(self.invoke.call_count, 2)

    def test_duplicate_worker_delivery_and_late_duplicate_after_replacement(self):
        self.immediate = True
        self.handle()
        old = self.queue[0]
        bridge.worker_handler(old, None)
        self.handle(request_id="second")
        self.assertIn("scaduta", speech(self.handle()))
        bridge.worker_handler(old, None)
        self.assertEqual(self.ask.call_count, 2)
        self.assertEqual(self.invoke.call_count, 2)

    def test_conversation_continues_across_result_new_launch_and_next_question(self):
        self.handle()
        self.finish()
        self.handle(name="AnswerStatusIntent", request_id="status", new=True)
        launch = event(new=True)
        launch["request"]["type"] = "LaunchRequest"
        bridge.lambda_handler(launch, None)
        self.handle(request_id="next", new=True)
        self.finish()
        self.assertEqual(self.ask.call_args.args[2], "conversation-2")
        self.assertNotIn("conversation_id", self.queue[-1])

    def test_user_skill_and_credential_isolation_even_with_forged_session_attributes(self):
        self.immediate = True
        self.handle()
        for kwargs in ({"user": "other"}, {"token": "rotated-token"}):
            other = event(name="AnswerStatusIntent", request_id="status", **kwargs)
            other["session"]["attributes"] = {"job_id": self.current()["job_id"], "conversation_id": "conversation-2"}
            self.assertIn("scaduta", speech(bridge.lambda_handler(other, None)))
        wrong_skill = event(name="AnswerStatusIntent")
        wrong_skill["context"]["System"]["application"]["applicationId"] = "other-skill"
        self.assertIn("non è autorizzato", speech(bridge.lambda_handler(wrong_skill, None)))
        self.ask.assert_called_once()

    def test_expiry_is_enforced_before_dynamodb_ttl_deletes_items(self):
        self.immediate = True
        self.handle()
        self.clock.sleep(bridge.JOB_SECONDS)
        self.assertIn("scaduta", speech(self.handle(name="AnswerStatusIntent", request_id="status")))
        self.assertTrue(self.current())  # Moto has not deleted the expired item.
        self.handle(request_id="fresh")
        self.assertIsNone(self.ask.call_args.args[2])

    def test_worker_error_and_http_authorization_are_explicit_and_not_retried(self):
        for error, card, expected in ((TimeoutError(), False, "Non posso confermare"),
                                      (HTTPError("https://private", 401, "secret", {}, None), True, "Collega"),
                                      (HTTPError("https://private", 403, "secret", {}, None), False, "non è autorizzato")):
            with self.subTest(error=error):
                self.ask.side_effect = error
                self.handle(request_id=str(id(error)))
                self.finish()
                reply = self.handle(name="AnswerStatusIntent", request_id="status")
                self.assertEqual(self.current()["status"], "FAILED")
                self.assertIn(expected, speech(reply))
                self.assertEqual("card" in reply["response"], card)
                count = self.ask.call_count
                self.finish()
                self.assertEqual(self.ask.call_count, count)

    def test_worker_crash_queue_loss_and_late_completion_fail_at_deadline(self):
        self.handle()
        self.clock.sleep(bridge.PROCESSING_SECONDS)
        self.assertIn("tempo massimo", speech(self.handle(name="AnswerStatusIntent", request_id="status")))
        self.finish()
        self.ask.assert_not_called()
        self.assertEqual(self.current()["status"], "FAILED")
        self.handle(request_id="next")
        def late(*_):
            self.clock.sleep(bridge.PROCESSING_SECONDS)
            return ANSWER
        self.ask.side_effect = late
        self.finish()
        self.assertIn("tempo massimo", speech(self.handle(name="AnswerStatusIntent", request_id="status")))
        self.assertNotIn("reply", self.current())

    def test_duplicate_while_running_cannot_claim_inference_twice(self):
        self.handle()
        def duplicate(*_):
            self.finish()
            return ANSWER
        self.ask.side_effect = duplicate
        self.finish()
        self.ask.assert_called_once()
        self.assertEqual(self.current()["status"], "DONE")

    def test_failed_transaction_does_not_create_an_orphan_marker_or_overwrite_winner(self):
        store = bridge._Jobs()
        winner = store.create("owner", "request-a", {})
        self.assertIsNone(store.create("owner", "request-b", {}))
        self.assertFalse(store.get("owner", "request#request-b"))
        self.assertEqual(store.get("owner")["job_id"], winner["job_id"])

    def test_frontend_reservation_conflict_returns_busy_without_dispatch(self):
        with patch.object(bridge._Jobs, "create", return_value=None):
            self.assertIn("non è stata inviata", speech(self.handle()))
        self.invoke.assert_not_called()

    def test_unexpected_worker_failure_is_saved_and_old_delivery_cannot_restart_after_ttl(self):
        original = event()
        bridge.lambda_handler(original, None)
        self.ask.side_effect = RuntimeError("private failure")
        self.finish()
        self.assertEqual(self.current()["status"], "FAILED")
        self.assertIn("non è riuscita", speech(self.handle(name="AnswerStatusIntent", request_id="status")))
        self.clock.sleep(bridge.JOB_SECONDS)
        self.assertIn("scaduta", speech(bridge.lambda_handler(original, None)))
        self.invoke.assert_called_once()

    def test_inference_failure_preserves_the_prior_conversation(self):
        self.immediate = True
        self.handle()
        self.ask.side_effect = TimeoutError()
        self.handle(request_id="failed")
        self.ask.side_effect = None
        self.handle(request_id="next")
        self.assertEqual(self.ask.call_args.args[2], "conversation-2")

    def test_oversized_answer_becomes_explicit_failure(self):
        self.immediate = True
        oversized = copy.deepcopy(ANSWER)
        oversized["response"]["speech"]["plain"]["speech"] = "あ" * 4000
        self.ask.return_value = oversized
        self.assertIn("non è riuscita", speech(self.handle()))
        self.assertEqual(self.current()["status"], "FAILED")
        self.handle(request_id="next")
        self.assertEqual(self.ask.call_args.args[2], "conversation-2")

    def test_ha_spoken_error_is_failed_and_allows_an_explicit_new_question(self):
        self.immediate = True
        failure = copy.deepcopy(ANSWER)
        failure["response"]["response_type"] = "error"
        failure["response"]["speech"]["plain"]["speech"] = "I could not find that device."
        self.ask.return_value = failure
        self.assertEqual(speech(self.handle()), "I could not find that device.")
        self.assertEqual(self.current()["status"], "FAILED")
        self.handle(request_id="next")
        self.assertEqual(self.invoke.call_count, 2)
        self.assertEqual(self.ask.call_args.args[2], "conversation-2")

    def test_ambiguous_dispatch_failure_is_not_retried(self):
        self.invoke.side_effect = TimeoutError("private-token")
        self.assertIn("non è riuscita", speech(self.handle()))
        self.handle()
        self.invoke.assert_called_once()
        self.clock.sleep(bridge.PROCESSING_SECONDS)
        self.assertIn("tempo massimo", speech(self.handle(name="AnswerStatusIntent", request_id="status")))
        self.ask.assert_not_called()

    def test_auth_configuration_missing_query_and_local_intents(self):
        for token in (None, "", "bad token", "bad\nheader", 23):
            reply = self.handle(token=token)
            self.assertEqual(reply["response"]["card"], {"type": "LinkAccount"})
        for key in ("BASE_URL", "AGENT_ID", "JOBS_TABLE", "WORKER_FUNCTION_ARN", "SKILL_ID"):
            with patch.dict(os.environ, {key: ""}):
                self.assertIn("non è riuscita", speech(self.handle()))
        missing = event()
        missing["request"]["intent"]["slots"] = {}
        self.assertIn("Non ho capito", speech(bridge.lambda_handler(missing, None)))
        self.assertTrue(self.handle(name="AMAZON.StopIntent")["response"]["shouldEndSession"])
        self.assertFalse(self.handle(name="AMAZON.HelpIntent")["response"]["shouldEndSession"])
        self.invoke.assert_not_called()

    def test_credentials_and_questions_are_not_stored_or_logged(self):
        self.immediate = True
        with self.assertLogs(bridge.LOGGER, level="INFO") as captured:
            self.handle()
        stored = json.dumps(self.db.scan(TableName="jobs")["Items"])
        logs = "\n".join(captured.output)
        for secret in ("linked-token", "Qual è la temperatura", "user-1", "In studio ci sono"):
            self.assertNotIn(secret, logs)
        for secret in ("linked-token", "Qual è la temperatura", "user-1"):
            self.assertNotIn(secret, stored)
        self.assertIn("FRONTEND_RETURN", logs)
        self.assertIn("elapsed_ms=", logs)

    def test_frontend_returns_while_independent_worker_is_still_running_wall_clock(self):
        # Threads emulate two Lambda invocations only in this test, never in production.
        self.clock_patch.stop()
        entered, release = threading.Event(), threading.Event()
        def inference(*_):
            entered.set()
            release.wait(10)
            return ANSWER
        self.ask.side_effect = inference
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            futures = []
            def dispatch(**kwargs):
                payload = json.loads(kwargs["Payload"])
                futures.append(executor.submit(bridge.worker_handler, payload, None))
                return {"StatusCode": 202}
            self.invoke.side_effect = dispatch
            start = time.monotonic()
            try:
                reply = self.handle()
                elapsed = time.monotonic() - start
                self.assertTrue(entered.is_set())
                self.assertIn("Sto preparando", speech(reply))
                self.assertLess(elapsed, 5.5)
                self.assertEqual(self.current()["status"], "RUNNING")
                self.assertFalse(futures[0].done())
                print(f"Wall-clock separation: acknowledgement={elapsed:.3f}s; worker still running")
            finally:
                release.set()
            futures[0].result(timeout=3)
        self.assertEqual(self.current()["status"], "DONE")


class TransportTests(unittest.TestCase):
    def test_missing_or_empty_returned_conversation_id_preserves_previous_context(self):
        for conversation_id in (None, "", "  "):
            with self.subTest(conversation_id=conversation_id):
                answer = {**ANSWER, "conversation_id": conversation_id}
                with patch.object(bridge, "_ask_ha", return_value=answer):
                    status, reply = bridge._infer("question", "en-GB", "previous", None, "token")
                self.assertEqual(status, "DONE")
                self.assertEqual(reply["sessionAttributes"], {"conversation_id": "previous"})

    def test_local_prompts_use_english_by_default_and_italian_when_requested(self):
        for locale, expected in ((None, "Sure, go ahead."), ("en-GB", "Sure, go ahead."),
                                 ("en-US", "Sure, go ahead."), ("it-IT", "Certo, dimmi pure."),
                                 ("de-DE", "Sure, go ahead.")):
            with self.subTest(locale=locale):
                request = {"type": "LaunchRequest"}
                if locale is not None:
                    request["locale"] = locale
                self.assertEqual(speech(bridge.lambda_handler({"request": request}, None)), expected)

    def test_worker_payload_token_context_and_bounded_socket_timeout(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(ANSWER).encode()
        with patch.dict(os.environ, ENV), patch.object(bridge.urllib.request, "build_opener", return_value=opener):
            status, reply = bridge._infer("  domanda  ", "it-IT", "previous", SimpleNamespace(get_remaining_time_in_millis=lambda: 120000), "linked-token")
        request = opener.open.call_args.args[0]
        self.assertEqual(json.loads(request.data), {"text": "  domanda  ", "language": "it-IT", "agent_id": ENV["AGENT_ID"], "conversation_id": "previous"})
        self.assertEqual(request.get_header("Authorization"), "Bearer linked-token")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 110)
        self.assertEqual(status, "DONE")
        self.assertEqual(reply["sessionAttributes"], {"conversation_id": "conversation-2"})

    def test_transport_errors_are_sanitized_explicit_and_never_retried(self):
        failures = [HTTPError("https://secret", 500, "secret", {}, None), TimeoutError("secret"),
                    URLError("secret"), ssl.SSLError("secret"), b'not json', b'\xff', b'[]',
                    b'{"response":null}', b'{"response":{"speech":null}}', b'{"response":{"speech":{"plain":{"speech":""}}}}']
        for failure in failures:
            with self.subTest(failure=failure):
                opener = MagicMock()
                if isinstance(failure, bytes):
                    opener.open.return_value.__enter__.return_value.read.return_value = failure
                else:
                    opener.open.side_effect = failure
                with patch.dict(os.environ, ENV), patch.object(bridge.urllib.request, "build_opener", return_value=opener), self.assertLogs(bridge.LOGGER, level="WARNING") as logs:
                    status, reply = bridge._infer("secret question", "it-IT", None, None, "secret-token")
                self.assertEqual(status, "FAILED")
                self.assertIn("Non posso confermare", speech(reply))
                self.assertNotIn("secret", "\n".join(logs.output))
                opener.open.assert_called_once()

    def test_url_validation_and_redirect_refusal(self):
        for url in ("http://ha.example", "https://ha.example/path", "https://user:pass@ha.example", "https://ha.example?x=secret", "https://ha.example#x"):
            with self.subTest(url=url), patch.dict(os.environ, {"BASE_URL": url}), patch.object(bridge.urllib.request, "build_opener") as opener:
                with self.assertRaises(ValueError):
                    bridge._post_ha("/api/conversation/process", {}, None, "token")
                opener.assert_not_called()
        self.assertIsNone(bridge._NoRedirect().redirect_request(None, None, 302, "", {}, "https://other"))

    def test_models_include_result_intent_and_existing_search_query(self):
        for locale in ("it-IT", "en-GB"):
            model = json.loads((Path("interaction_models") / (locale + ".json")).read_text())["interactionModel"]["languageModel"]
            intents = {intent["name"]: intent for intent in model["intents"]}
            self.assertEqual(intents["AskHomeIntent"]["slots"], [{"name": "query", "type": "AMAZON.SearchQuery"}])
            self.assertTrue(intents["AnswerStatusIntent"]["samples"])
            if locale == "it-IT":
                self.assertTrue({"è pronta", "hai la risposta", "dimmi la risposta"}.issubset(intents["AnswerStatusIntent"]["samples"]))
            else:
                self.assertTrue({"is it ready", "do you have the answer", "tell me the answer"}.issubset(intents["AnswerStatusIntent"]["samples"]))


if __name__ == "__main__":
    unittest.main()

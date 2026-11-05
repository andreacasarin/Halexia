"""Halexia Alexa bridge for Home Assistant conversation and Smart Home."""

import hashlib
from datetime import datetime
import json
import logging
import os
import re
import ssl
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request


MESSAGES = {
    "it": {
        "launch": "Certo, dimmi pure.",
        "help": "Puoi chiedermi qualcosa sulla casa, oppure chiedere: è pronta?",
        "stop": "A presto.",
        "missing": "Non ho capito la domanda. Riprova.",
        "link": "Collega il tuo account Home Assistant per continuare.",
        "permission": "Il tuo account non è autorizzato a usare questa funzione.",
        "error": "Non riesco a contattare Home Assistant. Non posso confermare se l'azione sia stata eseguita.",
        "fallback": "Non ho capito. Puoi chiedermi qualcosa sulla casa.",
        "pending": "Sto preparando la risposta. Tra qualche secondo chiedimi: è pronta?",
        "still_pending": "La risposta non è ancora pronta. Tra qualche secondo chiedimi di nuovo: è pronta?",
        "status_reprompt": "Puoi chiedermi di nuovo: è pronta?",
        "busy": "C'è già una domanda in corso. Questa nuova domanda non è stata inviata. Chiedimi: è pronta?",
        "ready": "La risposta precedente è pronta. Questa nuova domanda non è stata inviata. Chiedimi: dimmi la risposta.",
        "expired": "Non c'è una risposta disponibile, oppure è scaduta. Non ho ripetuto la domanda.",
        "failed": "La preparazione della risposta non è riuscita o ha superato il tempo massimo. Non ho ripetuto la domanda. Non posso confermare se l'azione sia stata eseguita.",
    },
    "en": {
        "launch": "Sure, go ahead.",
        "help": "You can ask me something about your home, or ask: is it ready?",
        "stop": "Goodbye.",
        "missing": "I didn't get your question. Please try again.",
        "link": "Link your Home Assistant account to continue.",
        "permission": "Your account is not allowed to use this feature.",
        "error": "I can't reach Home Assistant. I can't confirm whether the action was carried out.",
        "fallback": "I didn't understand. You can ask me something about your home.",
        "pending": "I'm preparing the answer. In a few seconds, ask me: is it ready?",
        "still_pending": "The answer isn't ready yet. In a few seconds, ask me again: is it ready?",
        "status_reprompt": "You can ask me again: is it ready?",
        "busy": "A question is already processing. This new question was not sent. Ask me: is it ready?",
        "ready": "The previous answer is ready. This new question was not sent. Ask me: tell me the answer.",
        "expired": "There is no answer available, or it has expired. I have not repeated the question.",
        "failed": "Preparing the answer failed or took too long. I have not repeated the question. I can't confirm whether the action was carried out.",
    },
}
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _locale(request):
    locale = request.get("locale", "en-GB")
    language = locale.split("-", 1)[0].lower()
    return locale, MESSAGES.get(language, MESSAGES["en"])


def _response(speech, end_session=False, conversation_id=None, link_account=False, reprompt=None):
    result = {
        "version": "1.0",
        "response": {
            "outputSpeech": {"type": "PlainText", "text": speech},
            "shouldEndSession": end_session,
        },
    }
    if conversation_id:
        result["sessionAttributes"] = {"conversation_id": conversation_id}
    if link_account:
        result["response"]["card"] = {"type": "LinkAccount"}
    if reprompt and not end_session:
        result["response"]["reprompt"] = {"outputSpeech": {"type": "PlainText", "text": reprompt}}
    return result


def _log_custom_failure(category, status=None, config=None):
    if not os.environ.get("DEBUG"):
        return
    if status is not None:
        LOGGER.warning("custom_failure category=%s status=%d", category, status)
    elif config is not None:
        LOGGER.warning("custom_failure category=%s config=%s", category, config)
    else:
        LOGGER.warning("custom_failure category=%s", category)


def _ha_url(path):
    url = os.environ["BASE_URL"].rstrip("/")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("BASE_URL must be a plain HTTPS base URL")
    return url + path


def _post_ha(path, payload, context, access_token, timeout=6):
    request = urllib.request.Request(
        _ha_url(path),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + access_token, "Content-Type": "application/json"},
        method="POST",
    )
    if context and hasattr(context, "get_remaining_time_in_millis"):
        timeout = min(timeout, max(0.1, context.get_remaining_time_in_millis() / 1000 - 1))
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _ask_ha(text, locale, conversation_id, context, access_token):
    payload = {"text": text, "language": locale, "agent_id": os.environ["AGENT_ID"]}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    return _post_ha("/api/conversation/process", payload, context, access_token, timeout=110)


def _valid_bearer_token(scope):
    if not isinstance(scope, dict) or scope.get("type") != "BearerToken":
        return None
    token = scope.get("token")
    return token if isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9\-._~+/]+=*", token) else None


def _smart_home_token(directive):
    payload = directive.get("payload")
    endpoint = directive.get("endpoint")
    scopes = []
    if isinstance(endpoint, dict) and "scope" in endpoint:
        scopes.append(endpoint["scope"])
    if isinstance(payload, dict) and "grantee" in payload:
        scopes.append(payload["grantee"])
    if isinstance(payload, dict) and "scope" in payload:
        scopes.append(payload["scope"])
    if not scopes:
        return None, "missing"
    token = _valid_bearer_token(scopes[0])
    return (token, None) if token else (None, "invalid")


def _smart_home_error(event, error_type, message):
    directive = event.get("directive")
    directive = directive if isinstance(directive, dict) else {}
    header = directive.get("header")
    header = header if isinstance(header, dict) else {}
    accept_grant = header.get("namespace") == "Alexa.Authorization" and header.get("name") == "AcceptGrant"
    response_header = {
        "namespace": "Alexa.Authorization" if accept_grant else "Alexa",
        "name": "ErrorResponse",
        "messageId": str(uuid.uuid4()),
        "payloadVersion": "3",
    }
    correlation_token = header.get("correlationToken")
    if not accept_grant and isinstance(correlation_token, str) and correlation_token:
        response_header["correlationToken"] = header["correlationToken"]
    result = {
        "event": {
            "header": response_header,
            "payload": {"type": "ACCEPT_GRANT_FAILED" if accept_grant else error_type, "message": message},
        }
    }
    endpoint = directive.get("endpoint")
    if isinstance(endpoint, dict) and isinstance(endpoint.get("endpointId"), str) and endpoint["endpointId"]:
        result["event"]["endpoint"] = {"endpointId": endpoint["endpointId"]}
    return result


def _handle_smart_home(event, context):
    directive = event.get("directive")
    if not isinstance(directive, dict):
        return _smart_home_error(event, "INVALID_DIRECTIVE", "The directive is invalid.")
    header = directive.get("header")
    payload = directive.get("payload")
    endpoint = directive.get("endpoint")
    if (
        not isinstance(header, dict)
        or not isinstance(header.get("namespace"), str)
        or not isinstance(header.get("name"), str)
        or not isinstance(header.get("messageId"), str)
        or header.get("payloadVersion") != "3"
        or not isinstance(payload, dict)
        or ("endpoint" in directive and not isinstance(endpoint, dict))
    ):
        return _smart_home_error(event, "INVALID_DIRECTIVE", "The directive is invalid.")

    token, token_error = _smart_home_token(directive)
    if token_error:
        error_type = "EXPIRED_AUTHORIZATION_CREDENTIAL" if token_error == "missing" else "INVALID_AUTHORIZATION_CREDENTIAL"
        return _smart_home_error(event, error_type, "The linked Home Assistant credential is unavailable or invalid.")
    try:
        return _post_ha("/api/alexa/smart_home", event, context, token)
    except urllib.error.HTTPError as error:
        if error.code == 401:
            return _smart_home_error(event, "INVALID_AUTHORIZATION_CREDENTIAL", "The Home Assistant credential is invalid.")
        if error.code == 403:
            return _smart_home_error(event, "INSUFFICIENT_PERMISSIONS", "The linked Home Assistant account lacks permission.")
        if error.code >= 500:
            error_type = "ENDPOINT_UNREACHABLE" if isinstance(endpoint, dict) else "INTERNAL_ERROR"
            return _smart_home_error(event, error_type, "Home Assistant could not process the directive.")
        return _smart_home_error(event, "INTERNAL_ERROR", "Home Assistant could not process the directive.")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, json.JSONDecodeError):
        error_type = "ENDPOINT_UNREACHABLE" if isinstance(endpoint, dict) else "INTERNAL_ERROR"
        return _smart_home_error(event, error_type, "Home Assistant could not be reached.")


def _infer(text, locale, previous_id, context, access_token):
    _, messages = _locale({"locale": locale})
    try:
        result = _ask_ha(text, locale, previous_id, context, access_token)
    except urllib.error.HTTPError as error:
        _log_custom_failure("http_error", status=error.code)
        if error.code == 401:
            return "FAILED", _response(messages["link"], link_account=True)
        if error.code == 403:
            return "FAILED", _response(messages["permission"])
        return "FAILED", _response(messages["error"], conversation_id=previous_id)
    except TimeoutError:
        _log_custom_failure("timeout")
        return "FAILED", _response(messages["error"], conversation_id=previous_id)
    except ssl.SSLError:
        _log_custom_failure("tls_error")
        return "FAILED", _response(messages["error"], conversation_id=previous_id)
    except urllib.error.URLError as error:
        if isinstance(error.reason, TimeoutError):
            _log_custom_failure("timeout")
        elif isinstance(error.reason, ssl.SSLError):
            _log_custom_failure("tls_error")
        else:
            _log_custom_failure("connection_error")
        return "FAILED", _response(messages["error"], conversation_id=previous_id)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _log_custom_failure("invalid_json")
        return "FAILED", _response(messages["error"], conversation_id=previous_id)
    except ValueError:
        _log_custom_failure("invalid_base_url", config="BASE_URL")
        return "FAILED", _response(messages["error"], conversation_id=previous_id)
    except OSError:
        _log_custom_failure("connection_error")
        return "FAILED", _response(messages["error"], conversation_id=previous_id)

    speech = None
    if isinstance(result, dict):
        response = result.get("response")
        speech_data = response.get("speech") if isinstance(response, dict) else None
        plain = speech_data.get("plain") if isinstance(speech_data, dict) else None
        speech = plain.get("speech") if isinstance(plain, dict) else None
    if not isinstance(speech, str) or not speech.strip():
        _log_custom_failure("unusable_speech")
        return "FAILED", _response(messages["error"], conversation_id=previous_id)
    conversation_id = result.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        conversation_id = previous_id
    status = "FAILED" if response.get("response_type") == "error" else "DONE"
    return status, _response(speech, conversation_id=conversation_id)


# Fixed bounds match deployment.yaml. TTL is retention, not the processing deadline.
JOB_SECONDS = 900
PROCESSING_SECONDS = 180
AWS_CALL_RESERVE = 0.8
STATUS_WAIT_SECONDS = 5.0
_AWS_CLIENTS = {}


class _BudgetExpired(Exception):
    pass


def _aws_client(service):
    # boto3 is included in the AWS Python runtime; local unit tests inject clients.
    import boto3
    from botocore.config import Config
    if service not in _AWS_CLIENTS:
        _AWS_CLIENTS[service] = boto3.client(service, config=Config(
            connect_timeout=0.2, read_timeout=0.35, retries={"total_max_attempts": 1},
        ))
    return _AWS_CLIENTS[service]


def _encode(item):
    return {key: ({"BOOL": value} if isinstance(value, bool) else
                  {"N": str(value)} if isinstance(value, int) else {"S": value})
            for key, value in item.items()}


def _decode(item):
    return {key: (int(value["N"]) if "N" in value else
                  value["BOOL"] if "BOOL" in value else value["S"])
            for key, value in item.items()}


def _conditional(error):
    response = getattr(error, "response", {})
    return (response.get("Error", {}).get("Code") == "ConditionalCheckFailedException" or
            any(reason.get("Code") == "ConditionalCheckFailed"
                for reason in response.get("CancellationReasons", [])))


class _Jobs:
    def __init__(self, deadline=None):
        self.deadline = deadline
        self.table = os.environ["JOBS_TABLE"]
        self.db = _aws_client("dynamodb")

    def call(self, method, **kwargs):
        if self.deadline is not None and time.monotonic() + AWS_CALL_RESERVE >= self.deadline:
            raise _BudgetExpired()
        return getattr(self.db, method)(TableName=self.table, **kwargs)

    def get(self, owner, key="current"):
        return _decode(self.call("get_item", Key=_encode({"owner": owner, "key": key}),
                                 ConsistentRead=True).get("Item", {}))

    def create(self, owner, request_id, previous):
        now = int(time.time())
        job = {"owner": owner, "key": "current", "job_id": uuid.uuid4().hex,
               "request_id": request_id, "status": "QUEUED", "delivered": False,
               "created_at": now, "deadline_at": now + PROCESSING_SECONDS,
               "expires_at": now + JOB_SECONDS}
        if previous:
            reply = json.loads(previous.get("reply", "{}"))
            conversation_id = reply.get("sessionAttributes", {}).get("conversation_id", previous.get("conversation_id"))
            if conversation_id:
                job["conversation_id"] = conversation_id
        marker = {"owner": owner, "key": "request#" + request_id,
                  "job_id": job["job_id"], "expires_at": now + JOB_SECONDS}
        values = _encode({":now": now})
        condition = "attribute_not_exists(#owner) OR expires_at <= :now"
        if previous:
            condition += " OR job_id = :old"
            values[":old"] = {"S": previous["job_id"]}
        # A conditional replacement serializes simultaneous questions for one user.
        if self.deadline is not None and time.monotonic() + AWS_CALL_RESERVE >= self.deadline:
            raise _BudgetExpired()
        try:
            self.db.transact_write_items(TransactItems=[
                {"Put": {"TableName": self.table, "Item": _encode(job),
                         "ConditionExpression": condition, "ExpressionAttributeNames": {"#owner": "owner"}, "ExpressionAttributeValues": values}},
                {"Put": {"TableName": self.table, "Item": _encode(marker),
                         "ConditionExpression": "attribute_not_exists(#owner) OR expires_at <= :now",
                         "ExpressionAttributeNames": {"#owner": "owner"}, "ExpressionAttributeValues": _encode({":now": now})}},
            ])
        except Exception as error:
            if _conditional(error):
                return None
            raise
        return job

    def update(self, job, changes, condition, values=None):
        names = {"#" + key: key for key in changes}
        names["#status"] = "status"
        encoded = _encode({":" + key: value for key, value in changes.items()})
        encoded.update(_encode({":job": job["job_id"], **(values or {})}))
        try:
            self.call("update_item", Key=_encode({"owner": job["owner"], "key": "current"}),
                      UpdateExpression="SET " + ", ".join("#" + key + " = :" + key for key in changes),
                      ConditionExpression="job_id = :job AND (" + condition + ")",
                      ExpressionAttributeNames=names, ExpressionAttributeValues=encoded)
        except Exception as error:
            if _conditional(error):
                return False
            raise
        job.update(changes)
        return True

    def reject(self, owner, request_id, message):
        try:
            self.call("put_item", Item=_encode({
                "owner": owner, "key": "request#" + request_id, "rejection": message,
                "expires_at": int(time.time()) + JOB_SECONDS,
            }), ConditionExpression="attribute_not_exists(#owner) OR expires_at <= :now",
                ExpressionAttributeNames={"#owner": "owner"},
                ExpressionAttributeValues=_encode({":now": int(time.time())}))
        except Exception as error:
            if not _conditional(error):
                raise


def _job_log(job_id, request_id, status, started):
    LOGGER.info("custom_job job=%s request=%s status=%s elapsed_ms=%d",
                job_id, request_id, status, (time.monotonic() - started) * 1000)


def _live_job(store, owner):
    job = store.get(owner)
    now = int(time.time())
    if not job or job["expires_at"] <= now:
        return {}
    if job["status"] in ("QUEUED", "RUNNING") and job["deadline_at"] <= now:
        if store.update(job, {"status": "FAILED"}, "#status IN (:queued, :running)",
                        {":queued": "QUEUED", ":running": "RUNNING"}):
            LOGGER.info("custom_job job=%s request=%s status=DEADLINE_FAILED age_ms=%d",
                        job["job_id"], job["request_id"], (now - job["created_at"]) * 1000)
        # Re-read if the worker finished concurrently with the timeout check.
        job = store.get(owner)
    return job


def _job_reply(store, job, messages, pending_message="pending"):
    if not job or job["expires_at"] <= int(time.time()):
        return _response(messages["expired"])
    if job["status"] in ("QUEUED", "RUNNING"):
        return _response(messages[pending_message], reprompt=messages["status_reprompt"])
    if "reply" not in job:
        return _response(messages["failed"])
    if job["status"] == "DONE" and not job["delivered"]:
        if not store.update(job, {"delivered": True}, "#status = :done",
                            {":done": "DONE"}):
            return _response(messages["expired"])
    return json.loads(job["reply"])


def _wait_for_job(store, owner, job, messages, pending_message="pending"):
    job_id = job.get("job_id")
    poll_deadline = store.deadline - 2 * AWS_CALL_RESERVE
    while job and job["status"] in ("QUEUED", "RUNNING"):
        remaining = poll_deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.15, remaining))
        job = _live_job(store, owner)
        if job.get("job_id") != job_id:
            return _response(messages["expired"])
    if job and job["status"] in ("QUEUED", "RUNNING"):
        LOGGER.info("custom_wait job=%s outcome=PENDING", job_id)
    return _job_reply(store, job, messages, pending_message)


def _custom_async(event, context, locale, messages, access_token, text=None):
    started = time.monotonic()
    try:
        budget = (STATUS_WAIT_SECONDS + 2 * AWS_CALL_RESERVE if text is None else
                  min(5.0, max(0, float(os.environ.get("RESPONSE_BUDGET_SECONDS", "5")))))
    except ValueError:
        _log_custom_failure("invalid_config", config="RESPONSE_BUDGET_SECONDS")
        return _response(messages["failed"])
    if context and hasattr(context, "get_remaining_time_in_millis"):
        budget = min(budget, max(0, context.get_remaining_time_in_millis() / 1000 - 1))
    deadline = started + budget
    user = event["context"]["System"]["user"]
    system = event["context"]["System"]
    skill_id = system.get("application", {}).get("applicationId")
    user_id = user.get("userId")
    request_id = event.get("request", {}).get("requestId")
    if not all(isinstance(value, str) and value for value in (skill_id, user_id, request_id)):
        return _response(messages["failed"])
    if skill_id != os.environ["SKILL_ID"]:
        return _response(messages["permission"])
    if text is not None:
        # Deduplication records outlive the accepted request age. An old Alexa
        # delivery cannot restart inference after its record has expired/deleted.
        try:
            timestamp = datetime.fromisoformat(event["request"]["timestamp"].replace("Z", "+00:00"))
            if timestamp.tzinfo is None or not -30 <= time.time() - timestamp.timestamp() < 300:
                return _response(messages["expired"])
        except (KeyError, TypeError, ValueError, AttributeError):
            return _response(messages["failed"])
    # Bind retained results to both the Alexa identity and this account-link credential.
    # A credential rotation starts a fresh namespace, without storing the credential.
    owner = hashlib.sha256(json.dumps([skill_id, user_id, access_token]).encode()).hexdigest()
    request_id = hashlib.sha256(request_id.encode()).hexdigest()
    job = {}
    try:
        store = _Jobs(deadline)
        marker = store.get(owner, "request#" + request_id) if text is not None else {}
        job = _live_job(store, owner)
        if text is None:
            return _wait_for_job(store, owner, job, messages, pending_message="still_pending")
        if marker and marker["expires_at"] > int(time.time()):
            if "rejection" in marker:
                return _response(messages[marker["rejection"]])
            # An old duplicate must never submit again or return a newer job's answer.
            return _job_reply(store, job if marker["job_id"] == job.get("job_id") else {}, messages)
        if job and (job["status"] in ("QUEUED", "RUNNING") or
                    (job["status"] == "DONE" and not job["delivered"])):
            message = "ready" if job["status"] == "DONE" else "busy"
            store.reject(owner, request_id, message)
            return _response(messages[message])
        job = store.create(owner, request_id, job) or {}
        if not job:
            # A concurrent invocation won the transaction. Do not submit this question.
            store.reject(owner, request_id, "busy")
            return _response(messages["busy"])
        _job_log(job["job_id"], request_id, "QUEUED", started)
        payload = {"owner": owner, "job_id": job["job_id"], "text": text,
                   "locale": locale, "access_token": access_token}
        worker = _aws_client("lambda")
        if time.monotonic() + AWS_CALL_RESERVE >= deadline:
            raise _BudgetExpired()
        # No retry here: an ambiguous Invoke result must never submit the job twice.
        result = worker.invoke(
            FunctionName=os.environ["WORKER_FUNCTION_ARN"], InvocationType="Event",
            Payload=json.dumps(payload).encode(),
        )
        if result["StatusCode"] != 202:
            raise RuntimeError("Worker invocation was not accepted")
        return _wait_for_job(store, owner, job, messages)
    except _BudgetExpired:
        # If a job was reserved but not dispatched, it will become FAILED at deadline.
        _job_log(job.get("job_id", "none"), request_id, "RESPONSE_BUDGET_EXPIRED", started)
        if job:
            return _response(messages["still_pending" if text is None else "pending"],
                             reprompt=messages["status_reprompt"])
        return _response(messages["failed"])
    except Exception:
        # SDK exceptions may contain request payloads. Never emit them or retry inference.
        _job_log(job.get("job_id", "none"), request_id, "AWS_FAILURE", started)
        return _response(messages["failed"])
    finally:
        _job_log(job.get("job_id", "none"), request_id, "FRONTEND_RETURN", started)


def worker_handler(event, context):
    """IAM-only endpoint; never configure an Alexa trigger on this function."""
    started = time.monotonic()
    job = {}
    try:
        store = _Jobs()
        job = store.get(event["owner"])
        now = int(time.time())
        if (job.get("job_id") != event["job_id"] or
                job.get("expires_at", 0) <= now or job.get("deadline_at", 0) <= now):
            return
        if not store.update(job, {"status": "RUNNING"}, "#status = :queued AND deadline_at > :now",
                            {":queued": "QUEUED", ":now": now}):
            return
        _job_log(job["job_id"], job["request_id"], "RUNNING", started)
        status, reply = _infer(event["text"], event["locale"], job.get("conversation_id"),
                               context, event["access_token"])
        # Bound the retained/Alexa payload; overlong speech is an explicit failure.
        if len(reply["response"]["outputSpeech"]["text"]) > 6000 or len(json.dumps(reply).encode()) > 20000:
            status = "FAILED"
            reply = _response(_locale({"locale": event["locale"]})[1]["failed"],
                              conversation_id=reply.get("sessionAttributes", {}).get("conversation_id"))
        saved = store.update(job, {"status": status, "reply": json.dumps(reply)},
                     "#status = :running AND deadline_at > :now",
                     {":running": "RUNNING", ":now": int(time.time())})
        _job_log(job["job_id"], job["request_id"], status if saved else "STALE_COMPLETION", started)
    except Exception:
        # Hard kills / store failures leave RUNNING; the frontend fails it at deadline.
        if job.get("status") == "RUNNING":
            try:
                reply = _response(_locale({"locale": event.get("locale", "en-GB")})[1]["failed"])
                store.update(job, {"status": "FAILED", "reply": json.dumps(reply)},
                             "#status = :running", {":running": "RUNNING"})
            except Exception:
                pass
        _job_log(job.get("job_id", "none"), job.get("request_id", "none"), "WORKER_FAILURE", started)


def _log_custom_request(request):
    request_type = request.get("type")
    if request_type not in ("LaunchRequest", "IntentRequest", "SessionEndedRequest"):
        request_type = "OTHER"
    intent_data = request.get("intent")
    intent = intent_data.get("name") if isinstance(intent_data, dict) else None
    if intent not in ("AskHomeIntent", "AnswerStatusIntent", "AMAZON.HelpIntent",
                      "AMAZON.FallbackIntent", "AMAZON.StopIntent", "AMAZON.CancelIntent"):
        intent = "OTHER"
    reason = request.get("reason")
    if reason not in ("USER_INITIATED", "ERROR", "EXCEEDED_MAX_REPROMPTS"):
        reason = "NONE"
    error_data = request.get("error")
    error_type = error_data.get("type") if isinstance(error_data, dict) else None
    if error_type not in ("INVALID_RESPONSE", "DEVICE_COMMUNICATION_ERROR",
                          "INTERNAL_SERVICE_ERROR", "ENDPOINT_TIMEOUT"):
        error_type = "NONE"
    raw_id = request.get("requestId")
    request_id = hashlib.sha256(raw_id.encode()).hexdigest() if isinstance(raw_id, str) else "none"
    age_ms = -1
    try:
        timestamp = datetime.fromisoformat(request["timestamp"].replace("Z", "+00:00"))
        if timestamp.tzinfo is not None:
            age_ms = int((time.time() - timestamp.timestamp()) * 1000)
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    LOGGER.info("custom_request request=%s type=%s intent=%s reason=%s error=%s age_ms=%d",
                request_id, request_type, intent, reason, error_type, age_ms)


def lambda_handler(event, context):
    if "directive" in event:
        return _handle_smart_home(event, context)
    request = event.get("request", {})
    _log_custom_request(request)
    if request.get("type") == "SessionEndedRequest":
        # Alexa does not accept speech or a skill response once a session ended.
        return None
    locale, messages = _locale(request)
    session = event.get("session", {})
    previous_id = None if session.get("new", False) else session.get("attributes", {}).get("conversation_id")
    name = request.get("intent", {}).get("name")
    if request.get("type") == "LaunchRequest":
        return _response(messages["launch"], conversation_id=previous_id)
    if request.get("type") != "IntentRequest":
        return _response(messages["fallback"], True, previous_id)
    if name in ("AMAZON.StopIntent", "AMAZON.CancelIntent"):
        return _response(messages["stop"], True)
    if name == "AMAZON.HelpIntent":
        return _response(messages["help"], conversation_id=previous_id)
    if name not in ("AskHomeIntent", "AnswerStatusIntent"):
        return _response(messages["fallback"], conversation_id=previous_id)
    text = request["intent"].get("slots", {}).get("query", {}).get("value") if name == "AskHomeIntent" else None
    if name == "AskHomeIntent" and (not isinstance(text, str) or not text.strip()):
        return _response(messages["missing"], conversation_id=previous_id)
    alexa_context = event.get("context")
    system = alexa_context.get("System") if isinstance(alexa_context, dict) else None
    user = system.get("user") if isinstance(system, dict) else None
    token = _valid_bearer_token({"type": "BearerToken", "token": user.get("accessToken")}) if isinstance(user, dict) else None
    if not token:
        return _response(messages["link"], link_account=True)
    for config in ("BASE_URL", "AGENT_ID", "JOBS_TABLE", "WORKER_FUNCTION_ARN", "SKILL_ID"):
        if not os.environ.get(config):
            _log_custom_failure("missing_config", config=config)
            return _response(messages["failed"])
    try:
        _ha_url("/api/conversation/process")
    except ValueError:
        _log_custom_failure("invalid_base_url", config="BASE_URL")
        return _response(messages["failed"])
    return _custom_async(event, context, locale, messages, token, text)

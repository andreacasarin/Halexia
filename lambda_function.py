"""Halexia Alexa bridge for Home Assistant conversation and Smart Home."""

import json
import logging
import os
import re
import ssl
import uuid
import urllib.error
import urllib.parse
import urllib.request


MESSAGES = {
    "it": {
        "launch": "Certo, dimmi pure.",
        "help": "Puoi chiedermi qualcosa sulla casa.",
        "stop": "A presto.",
        "missing": "Non ho capito la domanda. Riprova.",
        "link": "Collega il tuo account Home Assistant per continuare.",
        "permission": "Il tuo account non è autorizzato a usare questa funzione.",
        "error": "Non riesco a contattare Home Assistant. Non posso confermare se l'azione sia stata eseguita.",
        "fallback": "Non ho capito. Puoi chiedermi qualcosa sulla casa.",
    },
    "en": {
        "launch": "Sure, go ahead.",
        "help": "You can ask me something about your home.",
        "stop": "Goodbye.",
        "missing": "I didn't get your question. Please try again.",
        "link": "Link your Home Assistant account to continue.",
        "permission": "Your account is not allowed to use this feature.",
        "error": "I can't reach Home Assistant. I can't confirm whether the action was carried out.",
        "fallback": "I didn't understand. You can ask me something about your home.",
    },
}
LOGGER = logging.getLogger(__name__)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _locale(request):
    locale = request.get("locale", "en-GB")
    language = locale.split("-", 1)[0].lower()
    return locale, MESSAGES.get(language, MESSAGES["en"])


def _response(speech, end_session=False, conversation_id=None, link_account=False):
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


def _post_ha(path, payload, context, access_token):
    request = urllib.request.Request(
        _ha_url(path),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + access_token, "Content-Type": "application/json"},
        method="POST",
    )
    timeout = 6
    if context and hasattr(context, "get_remaining_time_in_millis"):
        timeout = min(timeout, max(0.1, context.get_remaining_time_in_millis() / 1000 - 1))
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _ask_ha(text, locale, conversation_id, session, context, access_token):
    payload = {"text": text, "language": locale, "agent_id": os.environ["AGENT_ID"]}
    if conversation_id and not session.get("new", False):
        payload["conversation_id"] = conversation_id
    return _post_ha("/api/conversation/process", payload, context, access_token)


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


def lambda_handler(event, context):
    if "directive" in event:
        return _handle_smart_home(event, context)
    request = event.get("request", {})
    locale, messages = _locale(request)
    session = event.get("session", {})
    attrs = session.get("attributes", {})
    previous_id = None if session.get("new", False) else attrs.get("conversation_id")
    request_type = request.get("type")
    if request_type == "LaunchRequest":
        return _response(messages["launch"], conversation_id=previous_id)
    if request_type != "IntentRequest":
        return _response(messages["fallback"], True, previous_id)

    intent = request.get("intent", {})
    name = intent.get("name")
    if name in ("AMAZON.StopIntent", "AMAZON.CancelIntent"):
        return _response(messages["stop"], True)
    if name == "AMAZON.HelpIntent":
        return _response(messages["help"], conversation_id=previous_id)
    if name != "AskHomeIntent":
        return _response(messages["fallback"], conversation_id=previous_id)

    text = intent.get("slots", {}).get("query", {}).get("value")
    if not isinstance(text, str) or not text.strip():
        return _response(messages["missing"], conversation_id=previous_id)
    alexa_context = event.get("context")
    system = alexa_context.get("System") if isinstance(alexa_context, dict) else None
    user = system.get("user") if isinstance(system, dict) else None
    access_token = user.get("accessToken") if isinstance(user, dict) else None
    if not isinstance(access_token, str) or not re.fullmatch(r"[A-Za-z0-9\-._~+/]+=*", access_token):
        return _response(messages["link"], link_account=True)
    if not os.environ.get("BASE_URL"):
        _log_custom_failure("missing_config", config="BASE_URL")
        return _response(messages["error"], conversation_id=previous_id)
    if not os.environ.get("AGENT_ID"):
        _log_custom_failure("missing_config", config="AGENT_ID")
        return _response(messages["error"], conversation_id=previous_id)
    try:
        result = _ask_ha(text, locale, previous_id, session, context, access_token)
    except urllib.error.HTTPError as error:
        _log_custom_failure("http_error", status=error.code)
        if error.code == 401:
            return _response(messages["link"], link_account=True)
        if error.code == 403:
            return _response(messages["permission"])
        return _response(messages["error"], conversation_id=previous_id)
    except TimeoutError:
        _log_custom_failure("timeout")
        return _response(messages["error"], conversation_id=previous_id)
    except ssl.SSLError:
        _log_custom_failure("tls_error")
        return _response(messages["error"], conversation_id=previous_id)
    except urllib.error.URLError as error:
        if isinstance(error.reason, TimeoutError):
            _log_custom_failure("timeout")
        elif isinstance(error.reason, ssl.SSLError):
            _log_custom_failure("tls_error")
        else:
            _log_custom_failure("connection_error")
        return _response(messages["error"], conversation_id=previous_id)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _log_custom_failure("invalid_json")
        return _response(messages["error"], conversation_id=previous_id)
    except ValueError:
        _log_custom_failure("invalid_base_url", config="BASE_URL")
        return _response(messages["error"], conversation_id=previous_id)
    except OSError:
        _log_custom_failure("connection_error")
        return _response(messages["error"], conversation_id=previous_id)

    speech = None
    if isinstance(result, dict):
        response = result.get("response")
        speech_data = response.get("speech") if isinstance(response, dict) else None
        plain = speech_data.get("plain") if isinstance(speech_data, dict) else None
        speech = plain.get("speech") if isinstance(plain, dict) else None
    if not isinstance(speech, str) or not speech.strip():
        _log_custom_failure("unusable_speech")
        return _response(messages["error"], conversation_id=previous_id)
    conversation_id = result.get("conversation_id")
    return _response(speech, conversation_id=conversation_id if isinstance(conversation_id, str) else None)

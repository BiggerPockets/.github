"""The small slice of Datadog's LLM Observability API the gym scripts share.

These are the experiments endpoints, split across an `unstable` and a `v2` prefix. Both
constants are here because Datadog moves them when the API stabilizes, and a 404 on every
call is the symptom. The routes and request shapes follow dd-trace-py's
`ddtrace/llmobs/_writer.py`, which is the only complete description of them.

A project is addressed by a path segment, never a body field: sending `project_id` in a
payload returns 200 and files the object under the org's default project instead.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

UNSTABLE = "/api/unstable/llm-obs/v1"
V2 = "/api/v2/llm-obs/v1"


class DatadogError(RuntimeError):
    pass


def credentials():
    """(site, api_key, app_key) from the environment. The application key is mandatory:
    these routes return 401 to an API key alone."""
    api_key = os.environ.get("DD_API_KEY")
    app_key = os.environ.get("DD_APP_KEY")
    if not api_key or not app_key:
        raise DatadogError("DD_API_KEY and DD_APP_KEY must be set")
    return os.environ.get("DD_SITE", "datadoghq.com"), api_key, app_key


def request_json(site, api_key, app_key, method, path, body=None):
    url = f"https://api.{site}{path}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "DD-API-KEY": api_key,
            "DD-APPLICATION-KEY": app_key,
            "Content-Type": "application/json",
        })
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:500]
        raise DatadogError(f"{method} {path} -> {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise DatadogError(f"{method} {path} failed: {error}") from error


def find_project(site, api_key, app_key, name):
    """The id of the project called `name`, or None.

    Datadog's filter is a contains-match on some deployments, so the exact name is
    re-checked here; picking a near-miss would silently write into the wrong project."""
    path = f"{UNSTABLE}/projects?filter%5Bname%5D={urllib.parse.quote(name)}"
    payload = request_json(site, api_key, app_key, "GET", path)
    for item in payload.get("data") or []:
        if ((item.get("attributes") or {}).get("name")) == name:
            return item.get("id")
    return None

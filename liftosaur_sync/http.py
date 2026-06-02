from __future__ import annotations

import json
import time

import httpx


USER_AGENT = "liftosaur-sync/2.0.0"
TIMEOUT = httpx.Timeout(30.0)
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def http_json(method: str, url: str, headers: dict[str, str], body: object | None = None) -> object:
    return http_json_with_retry(method, url, headers, body)


def http_form_json(method: str, url: str, body: dict[str, str]) -> object:
    with httpx.Client(timeout=TIMEOUT, headers=_base_headers()) as client:
        response = client.request(method, url, data=body)
        return _json_or_raise(response, method, url)


def http_multipart_json(
    method: str,
    url: str,
    headers: dict[str, str],
    fields: dict[str, str],
    file_field: str,
    filename: str,
    file_content: bytes,
    file_content_type: str,
) -> object:
    with httpx.Client(timeout=TIMEOUT, headers={**_base_headers(), **headers}) as client:
        response = client.request(
            method,
            url,
            data=fields,
            files={file_field: (filename, file_content, file_content_type)},
        )
        return _json_or_raise(response, method, url)


def http_json_with_retry(method: str, url: str, headers: dict[str, str], body: object | None) -> object:
    with httpx.Client(timeout=TIMEOUT, headers={**_base_headers(), **headers}) as client:
        for attempt in range(4):
            try:
                request_kwargs = {"json": body} if body is not None else {}
                response = client.request(method, url, **request_kwargs)
            except httpx.TransportError:
                if attempt == 3:
                    raise
                time.sleep(2**attempt)
                continue
            if response.status_code not in RETRYABLE_STATUSES or attempt == 3:
                return _json_or_raise(response, method, url)
            retry_after = response.headers.get("Retry-After")
            delay = int(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
            time.sleep(delay)
    raise RuntimeError(f"{method} {url} failed")


def _base_headers() -> dict[str, str]:
    return {"Accept": "application/json", "User-Agent": USER_AGENT}


def _json_or_raise(response: httpx.Response, method: str, url: str) -> object:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        raise RuntimeError(f"{method} {url} failed with HTTP {response.status_code}: {response.text}") from error
    return json.loads(response.text) if response.text else None

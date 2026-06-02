from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request


USER_AGENT = "liftosaur-sync/2.0.0"


def http_json(method: str, url: str, headers: dict[str, str], body: object | None = None) -> object:
    return http_json_with_retry(method, url, headers, body)


def http_form_json(method: str, url: str, body: dict[str, str]) -> object:
    data = urllib.parse.urlencode(body).encode()
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise RuntimeError(f"{method} {url} failed with HTTP {error.code}: {detail}") from error


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
    boundary = f"liftosaur-sync-{int(time.time() * 1000)}"
    parts: list[bytes] = []
    for key, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\nContent-Type: {file_content_type}\r\n\r\n'.encode()
        + file_content
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        url,
        data=b"".join(parts),
        headers={
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": USER_AGENT,
            **headers,
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise RuntimeError(f"{method} {url} failed with HTTP {error.code}: {detail}") from error


def http_json_with_retry(method: str, url: str, headers: dict[str, str], body: object | None) -> object:
    merged_headers = {"Accept": "application/json", "User-Agent": USER_AGENT, **headers}
    data = json.dumps(body).encode() if body is not None else None
    retryable_statuses = {429, 500, 502, 503, 504}
    for attempt in range(4):
        request = urllib.request.Request(url, data=data, headers=merged_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            if error.code not in retryable_statuses or attempt == 3:
                detail = error.read().decode(errors="replace")
                raise RuntimeError(f"{method} {url} failed with HTTP {error.code}: {detail}") from error
            retry_after = error.headers.get("Retry-After")
            delay = int(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
            time.sleep(delay)
        except (TimeoutError, ConnectionError, urllib.error.URLError):
            if attempt == 3:
                raise
            time.sleep(2**attempt)
    raise RuntimeError(f"{method} {url} failed")

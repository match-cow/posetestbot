"""Typed loopback HTTP client for the external cluster controller."""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Mapping


MAX_JSON_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_LOG_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_ARTIFACT_LIMIT_BYTES = 128 * 1024 * 1024


class ClusterClientError(RuntimeError):
    def __init__(self, message: str, *, status: int = 502):
        super().__init__(message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _validate_base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("Cluster controller URL must be loopback-only HTTP")
    port = parsed.port or 80
    host = f"[{parsed.hostname}]" if parsed.hostname == "::1" else parsed.hostname
    return f"http://{host}:{port}"


def new_idempotency_key(prefix: str) -> str:
    normalized = "".join(
        character if character.isalnum() or character in "._:-" else "-"
        for character in prefix
    ).strip("-")
    return f"{normalized[:40]}:{uuid.uuid4()}"


class ClusterControllerClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: float = 20.0,
    ):
        self.base_url = _validate_base_url(base_url)
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("Cluster controller token is not configured")
        self.__token = token
        self.timeout_seconds = timeout_seconds
        self.__opener = urllib.request.build_opener(_NoRedirect)

    def _url(self, path: str, query: Mapping[str, object] | None = None) -> str:
        if (
            not path.startswith(("/v1/", "/v2/"))
            or "\0" in path
            or "\n" in path
        ):
            raise ValueError("Controller path must use the versioned API")
        encoded = urllib.parse.urlencode(
            {
                key: str(value)
                for key, value in (query or {}).items()
                if value is not None
            }
        )
        return f"{self.base_url}{path}" + (f"?{encoded}" if encoded else "")

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        query: Mapping[str, object] | None = None,
        idempotency_key: str | None = None,
    ):
        encoded = (
            json.dumps(body, separators=(",", ":")).encode("utf-8")
            if body is not None
            else None
        )
        headers = {
            "Authorization": f"Bearer {self.__token}",
            "Accept": "application/json",
        }
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(
            self._url(path, query),
            data=encoded,
            headers=headers,
            method=method,
        )
        try:
            return self.__opener.open(request, timeout=self.timeout_seconds)
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read(MAX_JSON_RESPONSE_BYTES + 1)
                value = json.loads(raw) if len(raw) <= MAX_JSON_RESPONSE_BYTES else {}
                detail = value.get("detail") if isinstance(value, Mapping) else None
            except (OSError, ValueError, json.JSONDecodeError):
                detail = None
            raise ClusterClientError(
                str(detail or f"Cluster controller rejected the request ({exc.code})"),
                status=exc.code,
            ) from exc
        except (TimeoutError, urllib.error.URLError, socket.timeout, OSError) as exc:
            raise ClusterClientError("Cluster controller is unavailable") from exc

    def _json(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        query: Mapping[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        with self._request(
            method,
            path,
            body=body,
            query=query,
            idempotency_key=idempotency_key,
        ) as response:
            raw = response.read(MAX_JSON_RESPONSE_BYTES + 1)
        if len(raw) > MAX_JSON_RESPONSE_BYTES:
            raise ClusterClientError("Cluster controller response is too large")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ClusterClientError(
                "Cluster controller returned invalid JSON"
            ) from exc
        if not isinstance(value, dict):
            raise ClusterClientError("Cluster controller returned an invalid object")
        return value

    def status(self) -> dict[str, Any]:
        return self._json("GET", "/v1/status")

    def archives(self) -> dict[str, Any]:
        return self._json(
            "GET",
            "/v1/archives",
            query={"limit": 200},
        )

    def create_archive(
        self,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            "/v1/archives",
            body=payload,
            idempotency_key=idempotency_key,
        )

    def restore_archive(
        self,
        archive_id: str,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/archives/{urllib.parse.quote(archive_id, safe='')}/restore",
            body=payload,
            idempotency_key=idempotency_key,
        )

    def pose_jobs(
        self,
        *,
        limit: int = 50,
        before: str | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        return self._json(
            "GET",
            "/v1/pose-jobs",
            query={"limit": limit, "before": before, "state": state},
        )

    def create_pose_job(
        self,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            "/v1/pose-jobs",
            body=payload,
            idempotency_key=idempotency_key,
        )

    def estimators(self) -> dict[str, Any]:
        return self._json("GET", "/v2/estimators")

    def estimation_jobs(
        self,
        *,
        limit: int = 50,
        state: str | None = None,
    ) -> dict[str, Any]:
        return self._json(
            "GET",
            "/v2/estimation-jobs",
            query={"limit": limit, "state": state},
        )

    def create_estimation_job(
        self,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            "/v2/estimation-jobs",
            body=payload,
            idempotency_key=idempotency_key,
        )

    def job(self, job_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/jobs/{urllib.parse.quote(job_id, safe='')}")

    def cancel_job(self, job_id: str, *, idempotency_key: str) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/jobs/{urllib.parse.quote(job_id, safe='')}/cancel",
            body={},
            idempotency_key=idempotency_key,
        )

    def job_log(self, job_id: str) -> str:
        with self._request(
            "GET", f"/v1/jobs/{urllib.parse.quote(job_id, safe='')}/log"
        ) as response:
            raw = response.read(MAX_LOG_RESPONSE_BYTES + 1)
        if len(raw) > MAX_LOG_RESPONSE_BYTES:
            raise ClusterClientError("Cluster job log is too large")
        return raw.decode("utf-8", errors="replace")

    def download_artifact(
        self,
        job_id: str,
        artifact: str,
        destination: str | Path,
        *,
        max_bytes: int = DEFAULT_ARTIFACT_LIMIT_BYTES,
    ) -> Path:
        if artifact not in {"result.csv", "provenance.json"}:
            raise ValueError("Unknown cluster artifact")
        target = Path(destination)
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"Artifact destination already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with (
                self._request(
                    "GET",
                    f"/v1/jobs/{urllib.parse.quote(job_id, safe='')}/artifacts/{artifact}",
                ) as response,
                target.open("xb") as handle,
            ):
                total = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ClusterClientError(
                            "Cluster artifact exceeds the local import limit"
                        )
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            return target
        except Exception:
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            raise

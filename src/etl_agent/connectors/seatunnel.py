"""SeaTunnel REST API client.

A ``SeaTunnelClient`` wraps the SeaTunnel Zeta REST API (version
2.3.x) and exposes five operations:

- **submit_job**: submit a HOCON config and get a job_id.
- **get_job_status**: query the running state of a submitted job.
- **get_job_metrics**: retrieve cumulative counters (rows read,
  written, errors, bytes, duration).
- **stop_job**: cancel a running job.
- **get_job_error**: retrieve the error stack trace if a job failed.

Version routing:
    SeaTunnel 2.3.x uses the **legacy** Hazelcast REST API:
        POST /hazelcast/rest/maps/submit-job
        GET  /hazelcast/rest/maps/running-job/{jobId}
        GET  /hazelcast/rest/maps/job-info/{jobId}
        POST /hazelcast/rest/maps/stop-job/{jobId}

    SeaTunnel 2.4.x+ (Zeta engine v2) uses the **new** REST API:
        POST /submit-job
        GET  /running-jobs/{jobId}
        GET  /jobs/{jobId}/metrics
        POST /stop-job/{jobId}

The ``settings.seatunnel_api_version`` field ("legacy" or "v2")
controls which endpoint set is used.

Security: the HOCON config passed to ``submit_job`` contains
materialized credentials (from Vault).  These must never appear
in log output.  The client logs only the job_id and status, never
the HOCON body.
"""

from __future__ import annotations

import logging
import time
from enum import StrEnum
from typing import Any, cast

import requests

from etl_agent.settings import settings

logger = logging.getLogger(__name__)

# Default timeouts for SeaTunnel REST calls
_DEFAULT_TIMEOUT = 30  # seconds
_LONG_POLL_TIMEOUT = 60  # for job status queries
# Retry configuration for transient failures
_MAX_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 5


# =====================================================================
# Enums
# =====================================================================


class JobStatus(StrEnum):
    """SeaTunnel job status values (normalised across API versions)."""

    created = "created"
    running = "running"
    finished = "finished"
    canceled = "canceled"
    failed = "failed"
    unknown = "unknown"


class ApiVersion(StrEnum):
    """SeaTunnel REST API version selector."""

    legacy = "legacy"  # 2.3.x Hazelcast REST
    v2 = "v2"  # 2.4.x+ Zeta REST


# =====================================================================
# Data structures
# =====================================================================


class JobMetrics:
    """Normalised metrics snapshot from a SeaTunnel job.

    All counters are cumulative since job start.
    """

    def __init__(
        self,
        *,
        read_rows: int = 0,
        written_rows: int = 0,
        error_rows: int = 0,
        bytes_read: int = 0,
        bytes_written: int = 0,
        duration_seconds: float = 0.0,
        raw: dict[str, Any] | None = None,
    ) -> None:
        self.read_rows = read_rows
        self.written_rows = written_rows
        self.error_rows = error_rows
        self.bytes_read = bytes_read
        self.bytes_written = bytes_written
        self.duration_seconds = duration_seconds
        self.raw: dict[str, Any] = raw or {}

    @property
    def output_amplification(self) -> float:
        """written_rows / read_rows; >1 may indicate fan-out."""
        return self.written_rows / self.read_rows if self.read_rows > 0 else 0.0

    @property
    def error_rejection_rate(self) -> float:
        """error_rows / read_rows."""
        return self.error_rows / self.read_rows if self.read_rows > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a dict suitable for JSON storage."""
        return {
            "read_rows": self.read_rows,
            "written_rows": self.written_rows,
            "error_rows": self.error_rows,
            "bytes_read": self.bytes_read,
            "bytes_written": self.bytes_written,
            "duration_seconds": self.duration_seconds,
            "output_amplification": self.output_amplification,
            "error_rejection_rate": self.error_rejection_rate,
        }

    def __repr__(self) -> str:
        return (
            f"<JobMetrics(read={self.read_rows}, written={self.written_rows}, "
            f"errors={self.error_rows}, duration={self.duration_seconds:.1f}s)>"
        )


# =====================================================================
# Exceptions
# =====================================================================


class SeaTunnelError(Exception):
    """Base exception for SeaTunnel client errors."""


class SeaTunnelConnectionError(SeaTunnelError):
    """Failed to reach the SeaTunnel REST endpoint."""


class SeaTunnelJobNotFoundError(SeaTunnelError):
    """A job with the given ID does not exist on the SeaTunnel server."""


class SeaTunnelSubmitError(SeaTunnelError):
    """Job submission failed (e.g. invalid HOCON, server error)."""


class SeaTunnelTimeoutError(SeaTunnelError):
    """A REST call timed out after the configured retry limit."""


# =====================================================================
# Client
# =====================================================================


class SeaTunnelClient:
    """REST API client for SeaTunnel Zeta.

    The client is configured from ``settings`` (SEATUNNEL_HOST,
    SEATUNNEL_PORT, SEATUNNEL_API_VERSION).  It uses ``requests``
    for HTTP calls with configurable timeouts and retries.

    All methods raise ``SeaTunnelError`` subclasses on failure.
    The HOCON config body is never logged.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_version: str | ApiVersion | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = (
            base_url.rstrip("/") if base_url else settings.seatunnel_base_url.rstrip("/")
        )
        self._api_version = ApiVersion(
            api_version or settings.seatunnel_api_version,
        )
        self._timeout = timeout
        self._session = requests.Session()
        # Set default headers
        self._session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # URL routing
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        """Build a full URL for the given path."""
        return f"{self._base_url}{path}"

    def _submit_url(self) -> str:
        if self._api_version == ApiVersion.legacy:
            return self._url("/hazelcast/rest/maps/submit-job")
        return self._url("/submit-job")

    def _running_jobs_url(self, job_id: int) -> str:
        if self._api_version == ApiVersion.legacy:
            return self._url(f"/hazelcast/rest/maps/running-job/{job_id}")
        return self._url(f"/running-jobs/{job_id}")

    def _job_info_url(self, job_id: int) -> str:
        if self._api_version == ApiVersion.legacy:
            return self._url(f"/hazelcast/rest/maps/job-info/{job_id}")
        return self._url(f"/jobs/{job_id}")

    def _metrics_url(self, job_id: int) -> str:
        if self._api_version == ApiVersion.legacy:
            # Legacy API: metrics are embedded in job-info response
            return self._url(f"/hazelcast/rest/maps/job-info/{job_id}")
        return self._url(f"/jobs/{job_id}/metrics")

    def _stop_url(self, job_id: int) -> str:
        if self._api_version == ApiVersion.legacy:
            return self._url(f"/hazelcast/rest/maps/stop-job/{job_id}")
        return self._url(f"/stop-job/{job_id}")

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: int | None = None,
        retries: int = _MAX_RETRIES,
    ) -> dict[str, Any]:
        """Execute an HTTP request with retries.

        Retries on connection errors and 5xx server errors.
        Raises ``SeaTunnelConnectionError`` on persistent failure.
        """
        last_exc: Exception | None = None
        actual_timeout = timeout or self._timeout

        for attempt in range(1, retries + 1):
            try:
                resp = self._session.request(
                    method=method,
                    url=url,
                    json=json_body,
                    params=params,
                    timeout=actual_timeout,
                )

                # Retry on 5xx
                if resp.status_code >= 500:
                    last_exc = SeaTunnelConnectionError(
                        f"SeaTunnel returned HTTP {resp.status_code} for {method} {url}",
                    )
                    if attempt < retries:
                        logger.warning(
                            "SeaTunnel %s %s returned %d, retrying (%d/%d) in %ds",
                            method,
                            url,
                            resp.status_code,
                            attempt,
                            retries,
                            _RETRY_BACKOFF_SECONDS,
                        )
                        time.sleep(_RETRY_BACKOFF_SECONDS)
                        continue
                    raise last_exc

                # 404 -> job not found
                if resp.status_code == 404:
                    raise SeaTunnelJobNotFoundError(
                        f"SeaTunnel job not found at {url}",
                    )

                # Other 4xx -> client error (don't retry)
                if resp.status_code >= 400:
                    raise SeaTunnelError(
                        f"SeaTunnel returned HTTP {resp.status_code}: {resp.text[:512]}",
                    )

                # Success -- parse JSON
                try:
                    return cast(dict[str, Any], resp.json())
                except ValueError:
                    # Some SeaTunnel endpoints return plain text
                    return {"raw_text": resp.text}

            except requests.exceptions.Timeout as exc:
                last_exc = SeaTunnelTimeoutError(
                    f"SeaTunnel REST call timed out: {exc}",
                )
            except requests.exceptions.ConnectionError as exc:
                last_exc = SeaTunnelConnectionError(
                    f"Cannot connect to SeaTunnel at {url}: {exc}",
                )
            except (SeaTunnelJobNotFoundError, SeaTunnelError):
                raise
            except Exception as exc:
                last_exc = SeaTunnelError(f"Unexpected error: {exc}")

            # Retry on transient errors
            if attempt < retries:
                logger.warning(
                    "SeaTunnel request failed (attempt %d/%d): %s, retrying in %ds",
                    attempt,
                    retries,
                    str(last_exc)[:200],
                    _RETRY_BACKOFF_SECONDS,
                )
                time.sleep(_RETRY_BACKOFF_SECONDS)
            else:
                raise last_exc from last_exc

        # Should not reach here, but satisfy the type checker
        raise SeaTunnelError("Exhausted retries without response")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit_job(
        self,
        hocon_config: str,
        *,
        job_name: str | None = None,
    ) -> int:
        """Submit a HOCON config to SeaTunnel and return the job ID.

        Args:
            hocon_config: The HOCON configuration text.  Must contain
                materialized credentials -- never log this value.
            job_name: Optional name for the job.

        Returns:
            The SeaTunnel job ID (integer).

        Raises:
            SeaTunnelSubmitError: If submission fails (invalid HOCON,
                server error, etc.).
        """
        url = self._submit_url()
        body: dict[str, Any] = {"params": hocon_config}
        if job_name:
            body["jobName"] = job_name

        # CRITICAL: never log the hocon_config -- it contains credentials
        logger.info(
            "Submitting SeaTunnel job (name=%s, hocon_len=%d chars)",
            job_name or "<unnamed>",
            len(hocon_config),
        )

        try:
            resp = self._request("POST", url, json_body=body)
        except SeaTunnelError as exc:
            raise SeaTunnelSubmitError(
                f"Failed to submit SeaTunnel job: {exc}",
            ) from exc

        job_id = self._extract_job_id(resp)
        logger.info("SeaTunnel job submitted, job_id=%d", job_id)
        return job_id

    def get_job_status(self, job_id: int) -> JobStatus:
        """Query the current status of a submitted job.

        Returns:
            A ``JobStatus`` enum value.
        """
        # Try running-jobs first; if not found there, check job-info
        url = self._running_jobs_url(job_id)
        try:
            resp = self._request("GET", url, timeout=_LONG_POLL_TIMEOUT)
            # If the job is in the running-jobs map, it's running
            if resp and (resp.get("jobId") or resp.get("job_id")):
                return JobStatus.running
        except SeaTunnelJobNotFoundError:
            pass  # Not running -- check job-info for final status

        url = self._job_info_url(job_id)
        try:
            resp = self._request("GET", url, timeout=_LONG_POLL_TIMEOUT)
        except SeaTunnelJobNotFoundError:
            raise

        return self._parse_status(resp)

    def get_job_metrics(self, job_id: int) -> JobMetrics:
        """Retrieve cumulative metrics for a submitted job.

        Returns a ``JobMetrics`` with normalised counter names.
        """
        url = self._metrics_url(job_id)
        try:
            resp = self._request("GET", url, timeout=_LONG_POLL_TIMEOUT)
        except SeaTunnelJobNotFoundError:
            raise

        return self._parse_metrics(resp)

    def stop_job(self, job_id: int) -> bool:
        """Stop (cancel) a running job.

        Returns ``True`` if the stop request was accepted.
        """
        url = self._stop_url(job_id)
        try:
            # Legacy API uses POST with empty body
            self._request("POST", url, timeout=self._timeout)
        except SeaTunnelJobNotFoundError:
            logger.warning(
                "SeaTunnel job %d not found when trying to stop; it may have already finished.",
                job_id,
            )
            return False
        except SeaTunnelError as exc:
            logger.error(
                "Failed to stop SeaTunnel job %d: %s",
                job_id,
                str(exc)[:200],
            )
            return False

        logger.info("SeaTunnel job %d stop request sent", job_id)
        return True

    def get_job_error(self, job_id: int) -> str | None:
        """Retrieve the error message/stack trace for a failed job.

        Returns ``None`` if the job did not fail or no error
        information is available.
        """
        url = self._job_info_url(job_id)
        try:
            resp = self._request("GET", url, timeout=_LONG_POLL_TIMEOUT)
        except SeaTunnelJobNotFoundError:
            return None

        # Error info may be in different fields depending on version
        error_msg = resp.get("errorMessage") or resp.get("error_message") or resp.get("errorMsg")
        if error_msg:
            return str(error_msg)[:4096]

        # Check if there's a stack trace
        stack_trace = resp.get("stackTrace") or resp.get("stack_trace")
        if stack_trace:
            return str(stack_trace)[:4096]

        return None

    def health_check(self) -> bool:
        """Check if the SeaTunnel REST API is reachable.

        Returns ``True`` if the server responds with HTTP 200
        on the base URL.
        """
        try:
            resp = self._session.get(
                self._base_url,
                timeout=5,
            )
            return resp.status_code < 500
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Response parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_job_id(resp: dict[str, Any]) -> int:
        """Extract the job ID from a submit-job response."""
        # Legacy 2.3.x: {"jobId": 12345}
        # v2: {"jobId": 12345} or {"job_id": 12345}
        job_id = resp.get("jobId") or resp.get("job_id")
        if job_id is not None:
            try:
                return int(job_id)
            except (ValueError, TypeError) as exc:
                raise SeaTunnelSubmitError(
                    f"Invalid job_id in SeaTunnel response: {job_id}",
                ) from exc
        raise SeaTunnelSubmitError(
            f"SeaTunnel submit response missing jobId: {str(resp)[:256]}",
        )

    @staticmethod
    def _parse_status(resp: dict[str, Any]) -> JobStatus:
        """Parse the job status from a job-info response."""
        # Try multiple field names
        status_str = resp.get("jobStatus") or resp.get("job_status") or resp.get("status") or ""
        status_str = str(status_str).lower()

        # Normalise SeaTunnel status strings
        if status_str in ("finished", "done", "completed"):
            return JobStatus.finished
        if status_str in ("running", "in_progress"):
            return JobStatus.running
        if status_str in ("canceled", "cancelled", "canceled"):
            return JobStatus.canceled
        if status_str in ("failed", "error", "exception"):
            return JobStatus.failed
        if status_str in ("created", "new", "submitted"):
            return JobStatus.created
        return JobStatus.unknown

    @staticmethod
    def _parse_metrics(resp: dict[str, Any]) -> JobMetrics:
        """Parse metrics from a job-info or metrics response.

        SeaTunnel 2.3.x embeds counters in the job-info response:
            "counts": {"numReadSuccessful", "numWriteSuccessful", ...}

        SeaTunnel 2.4.x+ returns a separate metrics object.
        """
        # ---- Legacy 2.3.x: metrics embedded in job-info ----
        counts = resp.get("counts") or {}
        if counts:
            return JobMetrics(
                read_rows=int(counts.get("numReadSuccessful", 0))
                + int(counts.get("numReadFailed", 0)),
                written_rows=int(counts.get("numWriteSuccessful", 0)),
                error_rows=int(counts.get("numWriteFailed", 0)),
                bytes_read=0,  # not available in legacy
                bytes_written=0,  # not available in legacy
                duration_seconds=0.0,
                raw=resp,
            )

        # ---- v2 metrics object ----
        source = resp.get("source") or {}
        sink = resp.get("sink") or {}
        return JobMetrics(
            read_rows=int(
                source.get("totalReadCount", 0) or source.get("read_count", 0),
            ),
            written_rows=int(
                sink.get("totalWriteCount", 0) or sink.get("write_count", 0),
            ),
            error_rows=int(
                sink.get("totalWriteErrorCount", 0) or sink.get("error_count", 0),
            ),
            bytes_read=int(source.get("totalReadBytes", 0)),
            bytes_written=int(sink.get("totalWriteBytes", 0)),
            duration_seconds=float(
                resp.get("elapsedTime", 0) or resp.get("duration", 0.0),
            ),
            raw=resp,
        )

    def close(self) -> None:
        """Close the HTTP session."""
        self._session.close()

    def __enter__(self) -> SeaTunnelClient:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()


# =====================================================================
# Module-level singleton
# =====================================================================

_seatunnel_client: SeaTunnelClient | None = None


def get_seatunnel_client() -> SeaTunnelClient:
    """Return the process-wide ``SeaTunnelClient`` singleton."""
    global _seatunnel_client
    if _seatunnel_client is None:
        _seatunnel_client = SeaTunnelClient()
    return _seatunnel_client


def reset_seatunnel_client() -> None:
    """Discard the cached client (mainly for testing)."""
    global _seatunnel_client
    if _seatunnel_client is not None:
        _seatunnel_client.close()
    _seatunnel_client = None


__all__ = [
    "ApiVersion",
    "JobMetrics",
    "JobStatus",
    "SeaTunnelClient",
    "SeaTunnelConnectionError",
    "SeaTunnelError",
    "SeaTunnelJobNotFoundError",
    "SeaTunnelSubmitError",
    "SeaTunnelTimeoutError",
    "get_seatunnel_client",
    "reset_seatunnel_client",
]

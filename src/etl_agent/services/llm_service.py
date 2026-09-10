r"""LLM Provider adapter -- unified entry point for all model calls.

This module wraps the OpenAI-compatible chat-completions API and
provides three domain-specific operations used by the LangGraph
pipeline-generation chain:

1. ``parse_intent`` -- parse natural language into a structured
   ``PipelineRequirement``.
2. ``generate_etl_plan`` -- produce a structured ``EtlPlan`` from
   the requirement + real metadata profiles.
3. ``generate_hocon`` -- produce SeaTunnel HOCON text from an
   ``EtlPlan``.
4. ``suggest_fix`` -- given validation issues, produce a repair
   suggestion.

Design contract:

- **Retry & timeout**: each call retries up to ``max_retries`` times
  with exponential backoff.  Timeouts are enforced via
  ``httpx.Timeout``.
- **Structured output**: the LLM is asked to return JSON.  If the
  response cannot be parsed as the expected schema, the call is
  retried once with a stricter prompt.  If it still fails, a
  ``LLMResponseParseError`` is raised with a clear message.
- **Error handling**: network errors, timeout errors, and rate-limit
  errors are retried.  Authentication errors and invalid-request
  errors are raised immediately.
- **Mockable**: the ``LLMClient`` class is the single integration
  point.  Tests inject a mock via ``LLMService(client=MockClient())``
  or by monkey-patching ``get_llm_service``.

The service is **stateless** -- it holds no session or request
context.  All state lives in the LangGraph ``PipelineState``.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, cast

from openai import OpenAI
from openai.types.chat import ChatCompletion
from pydantic import ValidationError

from etl_agent.schemas.generation import EtlPlan, GeneratedHocon, ValidationIssue
from etl_agent.settings import settings

logger = logging.getLogger(__name__)

# =====================================================================
# Exceptions
# =====================================================================


class LLMError(Exception):
    """Base exception for LLM service failures."""


class LLMTimeoutError(LLMError):
    """The LLM call timed out after all retries."""


class LLMResponseParseError(LLMError):
    """The LLM response could not be parsed into the expected schema."""


class LLMAuthError(LLMError):
    """Authentication failed -- API key is invalid or missing."""


class LLMRateLimitError(LLMError):
    """The LLM provider returned a rate-limit error."""


# =====================================================================
# Client wrapper
# =====================================================================


class LLMClient:
    """Thin wrapper around the OpenAI SDK.

    Encapsulates client creation, timeout, and the ``chat.completions.create``
    call so that tests can mock a single method.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._api_key = api_key or settings.model_api_key
        self._base_url = base_url or settings.model_api_base
        self._model = model or settings.model_name
        self._timeout = timeout
        self._client: OpenAI | None = None

    @property
    def model(self) -> str:
        return self._model

    def _get_client(self) -> OpenAI:
        if self._client is None:
            if not self._api_key:
                raise LLMAuthError(
                    "MODEL_API_KEY is not set. Configure it in .env or environment variables."
                )
            self._client = OpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=self._timeout,
            )
        return self._client

    def chat_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        response_format_json: bool = False,
    ) -> str:
        """Send a chat-completion request and return the raw text.

        When ``response_format_json`` is True, the API is asked to
        return a JSON object (OpenAI ``response_format`` feature).
        """
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format_json:
            kwargs["response_format"] = {"type": "json_object"}

        response: ChatCompletion = client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content
        if content is None:
            raise LLMResponseParseError("LLM returned empty content")
        return content


# =====================================================================
# Service
# =====================================================================

# Default retry configuration
_MAX_RETRIES = 3
_RETRY_DELAYS = [1, 2, 4]  # seconds, exponential backoff
_PARSE_RETRY_LIMIT = 1  # one extra retry if JSON parsing fails


class LLMService:
    """Domain-specific LLM operations for the pipeline-generation chain.

    Each public method corresponds to a LangGraph node's LLM call.
    All methods are synchronous (the LangGraph nodes run in a worker
    thread or are called eagerly in tests).
    """

    def __init__(
        self,
        *,
        client: LLMClient | None = None,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        self._client = client or LLMClient()
        self._max_retries = max_retries

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    def parse_intent(self, natural_language: str) -> dict[str, Any]:
        """Parse natural language into a structured requirement dict.

        Returns a dict that can be validated into ``PipelineRequirement``.
        Missing fields are returned as ``None`` / empty so that the
        ``clarify`` node can detect them.
        """
        system_prompt = (
            "You are an ETL pipeline analyst. Parse the user's natural-language "
            "request into a structured JSON object with these fields:\n"
            "- sync_mode: 'full' or 'incremental' (infer from keywords)\n"
            "- source_table: source table name (string or null)\n"
            "- target_table: target table name (string or null)\n"
            "- source_schema: source schema/database (string or null)\n"
            "- target_schema: target schema/database (string or null)\n"
            "- primary_keys: list of primary key column names (may be empty)\n"
            "- watermark_column: column for incremental CDC (string or null)\n"
            "- extra_params: dict of additional hints parsed from text\n\n"
            "If a field cannot be inferred, set it to null (for strings) or "
            "empty list/dict. Do NOT invent table or column names.\n"
            "Return ONLY a JSON object, no markdown, no explanation."
        )
        raw = self._call_with_retry(
            system_prompt=system_prompt,
            user_prompt=f"User request: {natural_language}",
            response_format_json=True,
        )
        return self._parse_json(raw, context="parse_intent")

    def generate_etl_plan(
        self,
        requirement: dict[str, Any],
        source_metadata: dict[str, Any],
        target_metadata: dict[str, Any],
    ) -> EtlPlan:
        """Generate a structured EtlPlan from requirement + real metadata.

        ``source_metadata`` and ``target_metadata`` are dicts containing
        the real ``MetadataProfile`` data (table names, columns, types,
        nullable, comments, value distribution, masked samples).
        """
        system_prompt = (
            "You are an ETL pipeline architect. Based on the user's requirement "
            "and the REAL metadata profiles provided, generate a structured "
            "EtlPlan as JSON. You MUST use only real table and column names "
            "from the metadata -- never guess or invent names.\n\n"
            "The JSON object must have these top-level fields:\n"
            "- name: pipeline name (string)\n"
            "- description: short description (string)\n"
            "- source: {connection_id, database_name, schema_name, table_name}\n"
            "- target: {connection_id, database_name, schema_name, table_name, shadow_table}\n"
            "- field_mappings: list of {source_field, target_field, source_type, "
            "target_type, transform, nullable}\n"
            "- cleaning_rules: list of {field, rule, params}\n"
            "- sync_strategy: {mode, primary_keys, watermark_column, batch_size, "
            "parallelism, retry_times}\n"
            "- quality_contract: {max_error_ratio, dead_letter_table, dead_letter_schema, "
            "rules}\n\n"
            "Rules:\n"
            "1. Every field_mapping source_field MUST exist in the source metadata.\n"
            "2. Every field_mapping target_field MUST exist in the target metadata.\n"
            "3. For incremental sync, primary_keys MUST be provided.\n"
            "4. shadow_table should be set for full-sync swap strategies.\n"
            "5. quality_contract.dead_letter_table must be set.\n"
            "Return ONLY a JSON object."
        )
        user_prompt = (
            f"Requirement:\n{json.dumps(requirement, ensure_ascii=False, indent=2)}\n\n"
            f"Source metadata:\n{json.dumps(source_metadata, ensure_ascii=False, indent=2)}\n\n"
            f"Target metadata:\n{json.dumps(target_metadata, ensure_ascii=False, indent=2)}\n\n"
            "Generate the EtlPlan JSON:"
        )
        raw = self._call_with_retry(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_format_json=True,
            max_tokens=8192,
        )
        data = self._parse_json(raw, context="generate_etl_plan")

        # Local models often return null for optional numeric fields.
        # Normalize those values to the schema defaults before validation.
        sync_strategy = data.get("sync_strategy")
        if isinstance(sync_strategy, dict):
            sync_defaults = {
                "primary_keys": [],
                "batch_size": 1000,
                "parallelism": 1,
                "retry_times": 3,
            }
            for key, default in sync_defaults.items():
                if sync_strategy.get(key) is None:
                    sync_strategy[key] = default

        if data.get("cleaning_rules") is None:
            data["cleaning_rules"] = []

        try:
            return EtlPlan.model_validate(data)
        except ValidationError as exc:
            raise LLMResponseParseError(f"LLM output does not match EtlPlan schema: {exc}") from exc

    def generate_hocon(self, etl_plan: EtlPlan) -> GeneratedHocon:
        """Generate SeaTunnel HOCON from a structured EtlPlan.

        The LLM is given the EtlPlan as JSON and must return HOCON text
        wrapped in a JSON envelope so it can be parsed reliably.
        """
        system_prompt = (
            "You are a SeaTunnel configuration expert. Given a structured EtlPlan, "
            "generate a valid SeaTunnel HOCON configuration.\n\n"
            "Return a JSON object with these fields:\n"
            '- "config_text": the full HOCON string\n'
            '- "source_table_name": the source_name value used in the source block\n'
            '- "result_table_name": the result_table_name value used in the sink block\n'
            '- "plugin_info": {"source": "Jdbc", "sink": "Jdbc"}\n\n'
            "HOCON rules:\n"
            "1. Use 'env' block for connection credentials placeholders.\n"
            "2. source block: plugin_name='Jdbc', with source_name, result_table_name, "
            "query, fetch_size.\n"
            "3. sink block: plugin_name='Jdbc', with source_table_name (from result), "
            "database, table, primary_keys, mode.\n"
            "4. For incremental, set mode='incremental' and include watermark.\n"
            "5. Include a 'transform' block with SQL for field mapping and cleaning.\n"
            "Return ONLY a JSON object."
        )
        user_prompt = (
            f"EtlPlan:\n{etl_plan.model_dump_json(indent=2)}\n\nGenerate the SeaTunnel HOCON JSON:"
        )
        raw = self._call_with_retry(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_format_json=True,
            max_tokens=8192,
        )
        data = self._parse_json(raw, context="generate_hocon")
        try:
            return GeneratedHocon.model_validate(data)
        except ValidationError as exc:
            raise LLMResponseParseError(
                f"LLM output does not match GeneratedHocon schema: {exc}"
            ) from exc

    def suggest_fix(self, issues: list[ValidationIssue]) -> dict[str, Any]:
        """Given validation issues, produce a repair suggestion dict.

        Returns a dict like:
        ``{"etl_plan_patch": {...}, "hocon_patch": "..."}``
        or ``{"manual": True, "reason": "..."}`` when auto-fix is not
        possible.
        """
        system_prompt = (
            "You are an ETL pipeline repair assistant. Given a list of validation "
            "issues with severity, code, message, and suggestion, produce a "
            "repair plan.\n\n"
            "Return a JSON object:\n"
            '- "etl_plan_patch": partial EtlPlan fields to update\n'
            '- "hocon_patch": partial HOCON text to replace (or null)\n'
            '- "manual": true if the issues require human intervention\n'
            '- "reason": explanation when manual=true\n'
            "Return ONLY a JSON object."
        )
        issues_json = json.dumps([i.model_dump() for i in issues], ensure_ascii=False, indent=2)
        user_prompt = f"Validation issues:\n{issues_json}\n\nSuggest a fix:"
        raw = self._call_with_retry(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_format_json=True,
        )
        return self._parse_json(raw, context="suggest_fix")

    # -----------------------------------------------------------------
    # Internal: retry + parse
    # -----------------------------------------------------------------

    def _call_with_retry(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        response_format_json: bool = False,
    ) -> str:
        """Call the LLM with exponential-backoff retry.

        Retries on: timeout, rate-limit, and network errors.
        Does NOT retry on auth errors.
        """
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return self._client.chat_completion(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format_json=response_format_json,
                )
            except LLMAuthError:
                raise
            except (LLMRateLimitError, LLMTimeoutError) as exc:
                last_exc = exc
                logger.warning(
                    "LLM call failed (attempt %d/%d): %s",
                    attempt + 1,
                    self._max_retries + 1,
                    exc,
                )
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "LLM call error (attempt %d/%d): %s",
                    attempt + 1,
                    self._max_retries + 1,
                    exc,
                )
            if attempt < self._max_retries:
                delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
                time.sleep(delay)
        raise LLMError(
            f"LLM call failed after {self._max_retries + 1} attempts: {last_exc}"
        ) from last_exc

    def _parse_json(self, raw: str, *, context: str) -> dict[str, Any]:
        """Parse a raw LLM response into a JSON dict.

        If parsing fails, retry once with a stricter prompt asking
        the model to return pure JSON.  If it still fails, raise
        ``LLMResponseParseError``.
        """
        for attempt in range(_PARSE_RETRY_LIMIT + 1):
            try:
                # Strip markdown code fences if present
                cleaned = raw.strip()
                if cleaned.startswith("```"):
                    lines = cleaned.split("\n")
                    # Remove first and last fence lines
                    lines = [ln for ln in lines if not ln.strip().startswith("```")]
                    cleaned = "\n".join(lines)
                return cast(dict[str, Any], json.loads(cleaned))
            except (json.JSONDecodeError, ValueError) as exc:
                if attempt < _PARSE_RETRY_LIMIT:
                    logger.warning(
                        "JSON parse failed in %s (attempt %d): %s. Retrying...",
                        context,
                        attempt + 1,
                        exc,
                    )
                    # Retry with a stricter prompt
                    raw = self._client.chat_completion(
                        system_prompt=(
                            "Return ONLY a valid JSON object. "
                            "No markdown, no code fences, no explanation."
                        ),
                        user_prompt=f"Fix this to be valid JSON:\n{raw}",
                        temperature=0.0,
                        response_format_json=True,
                    )
                else:
                    raise LLMResponseParseError(
                        f"Failed to parse LLM response as JSON in {context} "
                        f"after {_PARSE_RETRY_LIMIT + 1} attempts. "
                        f"Raw response (truncated): {raw[:500]}"
                    ) from exc
        # Unreachable, but satisfies type checker
        raise LLMResponseParseError(f"Unexpected parse failure in {context}")


# =====================================================================
# Singleton accessor
# =====================================================================

_llm_service: LLMService | None = None


def get_llm_service() -> LLMService:
    """Return a cached ``LLMService`` instance.

    Tests can override this by setting ``_llm_service`` directly or
    by calling ``set_llm_service``.
    """
    global _llm_service
    if _llm_service is None:
        _llm_service = LLMService()
    return _llm_service


def set_llm_service(service: LLMService | None) -> None:
    """Inject a custom LLMService (used by tests for mocking)."""
    global _llm_service
    _llm_service = service


__all__ = [
    "LLMAuthError",
    "LLMClient",
    "LLMError",
    "LLMRateLimitError",
    "LLMResponseParseError",
    "LLMService",
    "LLMTimeoutError",
    "get_llm_service",
    "set_llm_service",
]

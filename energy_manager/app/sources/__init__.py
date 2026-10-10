"""Forecast and measurement sources (SPEC.md sections 4 and 7).

Shared helpers for provider modules: bounded HTTP JSON GET (stdlib only), provider error types and
declared-unit normalisation. Importing this package performs no I/O.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Mapping
log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_HTTP_TIMEOUT_S",
    "ProviderError",
    "RateLimited",
    "horizon_hours",
    "http_get_json",
    "unit_and_divisor",
]

DEFAULT_HTTP_TIMEOUT_S = 20.0
MIN_HORIZON_HOURS = 12
MAX_HORIZON_HOURS = 48


def horizon_hours(settings: Any, *, minimum: int = MIN_HORIZON_HOURS, maximum: int = MAX_HORIZON_HOURS) -> int:
    """``optimizer.horizon_hours`` clamped to the SPEC.md section 8.1 range (default 36)."""
    try:
        value = int(float(settings.get("optimizer.horizon_hours", 36)))
    except (TypeError, ValueError):
        value = 36
    return max(minimum, min(value, maximum))


class ProviderError(Exception):
    """A forecast provider failed; the caller may fall back to another provider."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RateLimited(ProviderError):
    """HTTP 429 from a provider; callers back off and serve cache."""

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


def http_get_json(
    url: str,
    *,
    timeout: float = DEFAULT_HTTP_TIMEOUT_S,
    headers: Mapping[str, str] | None = None,
) -> Any:
    """GET ``url`` and parse JSON, mapping HTTP failures to :class:`ProviderError`.

    Never logs the URL: personal API keys can be part of the path (SPEC.md R1).
    """
    request = urllib.request.Request(url, headers=dict(headers or {}), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as err:
        if err.code == 429:
            retry_after = err.headers.get("Retry-After") if err.headers else None
            seconds = None
            try:
                seconds = float(retry_after) if retry_after else None
            except (TypeError, ValueError):
                seconds = None
            raise RateLimited("provider rate limited (HTTP 429)", retry_after_s=seconds) from None
        raise ProviderError(f"provider request failed with HTTP {err.code}", status=err.code) from None
    except (urllib.error.URLError, TimeoutError) as err:
        raise ProviderError(f"provider request failed: {type(err).__name__}") from None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as err:
        raise ProviderError(f"provider returned invalid JSON: {err}") from None


#: Declared unit text -> (models PRICE_UNITS unit, scale divisor). Deliberately explicit: a unit we
#: do not recognise is an error, never a magnitude guess (SPEC.md section 4).
_PRICE_UNIT_TEXT: dict[str, tuple[str, float]] = {
    "eur/kwh": ("eur_per_kwh", 1.0),
    "€/kwh": ("eur_per_kwh", 1.0),
    "euro/kwh": ("eur_per_kwh", 1.0),
    "eur/mwh": ("eur_per_mwh", 1.0),
    "€/mwh": ("eur_per_mwh", 1.0),
    "eur_per_mwh": ("eur_per_mwh", 1.0),
    "ct/kwh": ("eur_per_kwh", 100.0),
    "cent/kwh": ("eur_per_kwh", 100.0),
    "cents/kwh": ("eur_per_kwh", 100.0),
    "micro_eur_per_kwh": ("micro_eur_per_kwh", 1.0),
    "µ€/kwh": ("micro_eur_per_kwh", 1.0),
    "ueur/kwh": ("micro_eur_per_kwh", 1.0),
}


def unit_and_divisor(declared: str | None) -> tuple[str, float] | None:
    """Map a provider's declared unit text to ``(unit, scale_divisor)``; ``None`` = unrecognised."""
    if not declared:
        return None
    key = str(declared).strip().lower().replace(" ", "")
    return _PRICE_UNIT_TEXT.get(key)

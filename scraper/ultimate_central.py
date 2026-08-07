"""Read-only client for the public Ultimate Central REST API.

The client preserves response provenance, applies a hard request budget, and
keeps restricted or structurally incomplete responses distinct from empty
successful collections. It never calls write endpoints.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

import requests

DEFAULT_BASE_URL = "https://euf.ultimatecentral.com"
MAX_PER_PAGE = 100
READ_ENDPOINTS = {
    "/api/help",
    "/api/events",
    "/api/games",
    "/api/teams",
    "/api/events/final_standings",
    "/api/persons",
}


class UltimateCentralError(RuntimeError):
    """Base class for client failures."""


class RequestBudgetExceeded(UltimateCentralError):
    pass


class UltimateCentralTransportError(UltimateCentralError):
    pass


class UltimateCentralEnvelopeError(UltimateCentralError):
    pass


@dataclass(frozen=True)
class APIResponse:
    status: int
    count: int
    result: list[dict[str, Any]]
    errors: list[Any]
    source_url: str
    observed_at: str
    payload_hash: str
    state: str

    @property
    def restricted(self) -> bool:
        return self.state == "restricted"


@dataclass(frozen=True)
class CollectionResult:
    items: list[dict[str, Any]]
    pages: list[APIResponse] = field(default_factory=list)
    state: str = "ok"

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def restricted(self) -> bool:
        return self.state == "restricted"


class UltimateCentralClient:
    """Budgeted, injectable, read-only Ultimate Central client."""

    def __init__(
        self,
        session: requests.Session,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 20.0,
        request_budget: int = 200,
        max_attempts: int = 3,
        backoff: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if request_budget < 1:
            raise ValueError("request_budget must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.request_budget = request_budget
        self.max_attempts = max_attempts
        self.backoff = backoff
        self.sleep = sleep
        self.requests_made = 0

    def _consume_budget(self) -> None:
        if self.requests_made >= self.request_budget:
            raise RequestBudgetExceeded(
                f"Ultimate Central request budget exhausted ({self.request_budget})"
            )
        self.requests_made += 1

    @staticmethod
    def _state(status: int, count: int, result: list[Any]) -> str:
        if status in (401, 403):
            return "restricted"
        if count > 0 and not result:
            return "incomplete"
        if count == 0:
            return "empty"
        return "ok"

    def _decode(self, response: requests.Response) -> APIResponse:
        raw = response.content
        observed = datetime.now(timezone.utc).isoformat()
        digest = hashlib.sha256(raw).hexdigest()
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise UltimateCentralEnvelopeError(
                f"non-JSON response from {response.url}"
            ) from exc
        if not isinstance(payload, dict):
            raise UltimateCentralEnvelopeError(
                f"response envelope from {response.url} is not an object"
            )

        missing = {"status", "count", "result", "errors"} - payload.keys()
        if missing:
            raise UltimateCentralEnvelopeError(
                f"response envelope from {response.url} is missing {sorted(missing)}"
            )
        status = payload["status"]
        count = payload["count"]
        result = payload["result"]
        errors = payload["errors"]
        if not isinstance(status, int) or not isinstance(count, int):
            raise UltimateCentralEnvelopeError("status and count must be integers")
        if not isinstance(result, list) or not isinstance(errors, list):
            raise UltimateCentralEnvelopeError("result and errors must be lists")
        return APIResponse(
            status=status,
            count=count,
            result=result,
            errors=errors,
            source_url=response.url,
            observed_at=observed,
            payload_hash=digest,
            state=self._state(status, count, result),
        )

    def get(self, endpoint: str, params: dict[str, Any] | None = None) -> APIResponse:
        if endpoint not in READ_ENDPOINTS:
            raise ValueError(f"endpoint is not an approved read endpoint: {endpoint}")
        url = self.base_url + endpoint
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            self._consume_budget()
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
                if attempt + 1 == self.max_attempts:
                    break
                self.sleep(self.backoff * (2**attempt))
                continue

            if response.status_code >= 500 or response.status_code == 429:
                last_error = UltimateCentralTransportError(
                    f"HTTP {response.status_code} from {response.url}"
                )
                if attempt + 1 == self.max_attempts:
                    break
                retry_after = response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    delay = float(retry_after)
                elif response.status_code == 429:
                    delay = max(10.0, self.backoff * (2**attempt))
                else:
                    delay = self.backoff * (2**attempt)
                self.sleep(delay)
                continue
            if response.status_code in (401, 403):
                try:
                    decoded = self._decode(response)
                except UltimateCentralEnvelopeError:
                    return APIResponse(
                        status=response.status_code,
                        count=0,
                        result=[],
                        errors=[f"HTTP {response.status_code}"],
                        source_url=response.url,
                        observed_at=datetime.now(timezone.utc).isoformat(),
                        payload_hash=hashlib.sha256(response.content).hexdigest(),
                        state="restricted",
                    )
                return APIResponse(**{**decoded.__dict__, "state": "restricted"})
            if 400 <= response.status_code:
                raise UltimateCentralTransportError(
                    f"permanent HTTP {response.status_code} from {response.url}"
                )
            return self._decode(response)
        raise UltimateCentralTransportError(str(last_error or "request failed"))

    def _pages(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        per_page: int = 100,
    ) -> CollectionResult:
        if not 1 <= per_page <= MAX_PER_PAGE:
            raise ValueError(f"per_page must be between 1 and {MAX_PER_PAGE}")
        base = dict(params or {})
        items: list[dict[str, Any]] = []
        pages: list[APIResponse] = []
        page = 1
        expected: int | None = None
        while True:
            response = self.get(
                endpoint, {**base, "page": page, "per_page": per_page}
            )
            pages.append(response)
            if response.restricted:
                return CollectionResult(items, pages, "restricted")
            if response.state == "incomplete":
                return CollectionResult(items, pages, "incomplete")
            if expected is None:
                expected = response.count
            items.extend(response.result)
            if not response.result or len(items) >= expected:
                break
            page += 1
        state = "empty" if not items else "ok"
        return CollectionResult(items, pages, state)

    def get_help(self) -> APIResponse:
        return self.get("/api/help")

    def list_events(self, per_page: int = 100, **filters: Any) -> CollectionResult:
        return self._pages("/api/events", filters, per_page)

    def list_games(self, event_id: str | int, per_page: int = 50) -> CollectionResult:
        return self._pages("/api/games", {"event_id": str(event_id)}, per_page)

    def list_teams(self, event_id: str | int, per_page: int = 100) -> CollectionResult:
        return self._pages("/api/teams", {"event_id": str(event_id)}, per_page)

    def final_standings(self, event_id: str | int) -> APIResponse:
        return self.get("/api/events/final_standings", {"event_id": str(event_id)})

    def list_public_persons(
        self, event_id: str | int, per_page: int = 50
    ) -> CollectionResult:
        return self._pages(
            "/api/persons",
            {"event_id": str(event_id), "event_roster_view_modes": "public"},
            per_page,
        )


def page_hashes(result: CollectionResult | APIResponse) -> Iterable[tuple[str, str, str]]:
    """Yield (URL, observation time, hash) for provenance storage."""
    pages = result.pages if isinstance(result, CollectionResult) else [result]
    for page in pages:
        yield page.source_url, page.observed_at, page.payload_hash

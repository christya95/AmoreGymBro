"""Read-only client for the documented Hevy API.

Write endpoints exist in Hevy's docs. This client does not call them.
GET /v1/workouts/{workoutId} returns one saved Workout object.
"""

from __future__ import annotations

import time
import uuid
from typing import Callable, Iterator

import httpx

MAX_PAGES = 10000
PAGE_SIZE = 10
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _collection(payload: dict, collection: str, path: str) -> list:
    if collection in payload:
        items = payload[collection]
        if not isinstance(items, list):
            raise HevyError(f"Hevy response for {path} did not include a list.")
        return items
    # Observed when no workout events exist: the page is empty and the body
    # uses a workouts list instead of events.
    if collection == "events" and payload.get("workouts") == []:
        return []
    if collection == "body_measurements" and "body_measurements" not in payload:
        return []
    raise HevyError(f"Hevy response for {path} did not include {collection}.")


class HevyError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class HevyAuthError(HevyError):
    pass


class HevyClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.hevyapp.com",
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 5,
        page_delay: float = 0.25,
    ):
        if not api_key or not str(api_key).strip():
            raise HevyAuthError("HEVY_API_KEY is not set in the local environment.")
        self.base_url = base_url.rstrip("/")
        self._api_key = str(api_key).strip()
        self._http = http or httpx.Client()
        self._owns_http = http is None
        self._sleep = sleep
        self.max_attempts = max_attempts
        self.page_delay = page_delay

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def get_workout_count(self) -> int:
        payload = self._get("/v1/workouts/count")
        return int(payload["workout_count"])

    def get_user(self) -> dict:
        payload = self._get("/v1/user/info")
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, dict) else {}

    def get_workout(self, workout_id: str) -> dict:
        """Read one saved workout. The documented response is the Workout object itself."""
        try:
            canonical = str(uuid.UUID(str(workout_id)))
        except ValueError:
            raise HevyError("Workout id is not valid.", status_code=400) from None
        payload = self._get(f"/v1/workouts/{canonical}")
        candidate = payload
        if "id" not in payload and isinstance(payload.get("workout"), dict):
            candidate = payload["workout"]
        if not isinstance(candidate, dict) or not candidate.get("id"):
            raise HevyError("Hevy did not return a workout.")
        return candidate

    def iter_workouts(self) -> Iterator[dict]:
        yield from self._iter_pages("/v1/workouts", "workouts")

    def iter_routines(self) -> Iterator[dict]:
        """All saved templates, including routines outside the app-managed folder."""
        yield from self._iter_pages("/v1/routines", "routines")

    def iter_routine_folders(self) -> Iterator[dict]:
        yield from self._iter_pages("/v1/routine_folders", "routine_folders")

    def iter_events(self, since: str) -> Iterator[dict]:
        yield from self._iter_pages("/v1/workouts/events", "events", {"since": since})

    def iter_body_measurements(self) -> Iterator[dict]:
        """Saved body measurements. Hevy's documented record has no height field."""
        yield from self._iter_pages("/v1/body_measurements", "body_measurements")

    def _iter_pages(self, path: str, collection: str, extra: dict | None = None) -> Iterator[dict]:
        page = 1
        while page <= MAX_PAGES:
            if page > 1 and self.page_delay:
                self._sleep(self.page_delay)
            params = {"page": page, "pageSize": PAGE_SIZE}
            if extra:
                params.update(extra)
            payload = self._get(path, params)
            items = _collection(payload, collection, path)
            page_count = int(payload.get("page_count") or 0)
            for item in items:
                yield item
            if not items:
                # A later page is still promised, so an empty page would drop workouts.
                # An empty first-and-last page is a real empty result. Hevy's event
                # feed does that when nothing changed, and omits the events list.
                if page_count > page:
                    raise HevyError(
                        f"Hevy page {page} of {path} was empty before page {page_count}."
                    )
                return
            if page >= page_count:
                return
            page += 1
        raise HevyError(f"Hevy pagination for {path} exceeded {MAX_PAGES} pages.")

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        headers = {"api-key": self._api_key, "accept": "application/json"}
        last_status: int | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._http.get(url, params=params, headers=headers, timeout=30.0)
            except httpx.HTTPError:
                if attempt == self.max_attempts:
                    raise HevyError("Hevy could not be reached after several tries.") from None
                self._sleep(self._backoff(attempt))
                continue
            last_status = response.status_code
            if response.status_code in RETRYABLE_STATUS and attempt < self.max_attempts:
                self._sleep(self._retry_delay(response, attempt))
                continue
            if response.status_code == 401:
                raise HevyAuthError(
                    "Hevy rejected the API key (HTTP 401). Check HEVY_API_KEY in the local .env file."
                ) from None
            if response.status_code >= 400:
                raise HevyError(
                    f"Hevy returned HTTP {response.status_code}.",
                    status_code=response.status_code,
                ) from None
            try:
                payload = response.json()
            except ValueError:
                raise HevyError("Hevy returned a response that was not JSON.") from None
            if not isinstance(payload, dict):
                raise HevyError("Hevy returned a response that was not a JSON object.")
            return payload
        raise HevyError(
            f"Hevy returned HTTP {last_status} after several tries.",
            status_code=last_status,
        )

    def _backoff(self, attempt: int) -> float:
        return min(8.0, 0.5 * (2 ** (attempt - 1)))

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        header = response.headers.get("retry-after")
        if header:
            try:
                return min(30.0, float(header))
            except ValueError:
                pass
        return self._backoff(attempt)

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

MAX_BULK = 500


@dataclass
class SyncReport:
    total: int = 0
    created: int = 0
    updated: int = 0
    failed: int = 0
    failures: List[Dict[str, Any]] = field(default_factory=list)
    results: List[Dict[str, Any]] = field(default_factory=list)

    def add(self, results: Iterable[Dict[str, Any]]) -> None:
        for r in results:
            self.total += 1
            if r["outcome"] == "CREATED":
                self.created += 1
            elif r["outcome"] == "UPDATED":
                self.updated += 1
            else:
                self.failed += 1
                self.failures.append({"externalId": r["externalId"], "error": r.get("error")})
            self.results.append(r)


class Syncer:
    """Keeps one kind of record in sync with the platform.

    ``init(records)`` is the first full load (chunked, with a report), ``push`` / ``push_many`` send changes as
    they happen (upsert, so replays are harmless) and ``remove`` sends a delete. ``map(record)`` must return the
    push body including ``externalId``.
    """

    def __init__(
        self,
        resource,
        kind: str,
        map: Callable[[Any], Dict[str, Any]],  # noqa: A002 - same name as the other SDKs
        chunk_size: int = 200,
        on_progress: Optional[Callable[[SyncReport], None]] = None,
    ):
        self._resource = resource
        self._kind = kind
        self._map = map
        self._chunk = min(chunk_size, MAX_BULK)
        self._on_progress = on_progress

    def init(self, records: Iterable[Any]) -> SyncReport:
        """Accepts any iterable, so a big table can be streamed (a generator over a cursor)."""
        report = SyncReport()
        batch: List[Dict[str, Any]] = []

        def flush() -> None:
            if not batch:
                return
            report.add(self._resource.bulk(batch, chunk_size=self._chunk))
            batch.clear()
            if self._on_progress:
                self._on_progress(report)

        for record in records:
            batch.append(self._mapped(record))
            if len(batch) >= self._chunk:
                flush()
        flush()
        return report

    def push(self, record: Any) -> Dict[str, Any]:
        return self._resource.upsert(self._mapped(record))

    def push_many(self, records: Iterable[Any]) -> SyncReport:
        report = SyncReport()
        report.add(self._resource.bulk([self._mapped(r) for r in records], chunk_size=self._chunk))
        return report

    def remove(self, record: Any) -> Dict[str, Any]:
        push = self._mapped(record)
        flag = {"deleted": True} if self._kind == "facilities" else {"status": "DELETED"}
        return self._resource.upsert({**push, **flag})

    def _mapped(self, record: Any) -> Dict[str, Any]:
        push = self._map(record)
        if not push or not push.get("externalId"):
            raise ValueError("map(record) must return a dict with an externalId")
        return push

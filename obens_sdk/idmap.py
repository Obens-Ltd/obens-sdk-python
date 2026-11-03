from __future__ import annotations

from typing import Dict, Iterable, Optional

from .http import ObensApiError


class IdMap:
    """Your id <-> platform id for one kind (SUB_ACTOR, FACILITY or ACCOUNT), cached after ``load()``."""

    def __init__(self, client, type_: str):
        self._client = client
        self.type = type_
        self._to_id: Dict[str, str] = {}
        self._to_external: Dict[str, str] = {}

    def load(self) -> "IdMap":
        """Downloads the whole table once."""
        self._to_id.clear()
        self._to_external.clear()
        for m in self._client.mappings.list(self.type):
            self._put(m["externalId"], m["id"])
        return self

    def internal(self, external_id: str) -> Optional[str]:
        """The platform id for your id, or None when it is unknown."""
        if external_id not in self._to_id:
            m = self._lookup(lambda: self._client.mappings.to_internal(self.type, external_id))
            if m:
                self._put(m["externalId"], m["id"])
        return self._to_id.get(external_id)

    def external(self, id_: str) -> Optional[str]:
        """Your id for a platform id, or None when the record has none."""
        if id_ not in self._to_external:
            m = self._lookup(lambda: self._client.mappings.to_external(self.type, id_))
            if m:
                self._put(m["externalId"], m["id"])
        return self._to_external.get(id_)

    def remember(self, results: Iterable[dict]) -> None:
        """Keeps the ids of a push without another request."""
        for r in results:
            if r.get("id"):
                self._put(r["externalId"], r["id"])

    def _put(self, external_id: str, id_: str) -> None:
        self._to_id[external_id] = id_
        self._to_external[id_] = external_id

    @staticmethod
    def _lookup(call):
        try:
            return call()
        except ObensApiError as e:
            if e.status == 404:
                return None
            raise

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

from .http import Http
from .idmap import IdMap
from .sync import MAX_BULK, Syncer


def _enc(value: str) -> str:
    return quote(str(value), safe="")


class Resource:
    def __init__(self, http: Http, path: str):
        self._http = http
        self._path = path

    def upsert(self, push: Dict[str, Any]) -> Dict[str, Any]:
        """Creates the record the first time ``externalId`` is seen, updates it afterwards. Safe to repeat."""
        if not push.get("externalId"):
            raise ValueError("externalId is required")
        return self._http.request("PUT", f"{self._path}/{_enc(push['externalId'])}", push)

    def bulk(self, items: List[Dict[str, Any]], chunk_size: int = MAX_BULK) -> List[Dict[str, Any]]:
        """Upserts many in requests of at most 500. One result per item, in order, failures included."""
        size = min(chunk_size, MAX_BULK)
        results: List[Dict[str, Any]] = []
        for i in range(0, len(items), size):
            results.extend(self._http.request("POST", f"{self._path}/bulk", {"items": items[i : i + size]}))
        return results

    def get(self, external_id: str, sub_actor: Optional[str] = None) -> Dict[str, Any]:
        """``sub_actor``: the external id of the farmer a facility belongs to (facility ids are unique per owner)."""
        query = f"?sub-actor={_enc(sub_actor)}" if sub_actor else ""
        return self._http.request("GET", f"{self._path}/{_enc(external_id)}{query}")


class _Accounts:
    def __init__(self, http: Http):
        self._http = http

    def accessible(self) -> List[Dict[str, Any]]:
        """The key's own account and the accounts delegated to it: id, name, self."""
        return self._http.request("GET", "/integration/accounts")

    def link(self, external_id: str, account_id: str) -> Dict[str, Any]:
        """Links your id to an account that already exists on the platform."""
        return self._http.request("PUT", f"/integration/accounts/{_enc(external_id)}", {"accountId": account_id})

    def unlink(self, external_id: str) -> None:
        self._http.request("DELETE", f"/integration/accounts/{_enc(external_id)}")


class _Mappings:
    def __init__(self, http: Http):
        self._http = http

    def list(self, type_: str) -> List[Dict[str, Any]]:
        return self._http.request("GET", f"/integration/mappings/{type_}")

    def to_internal(self, type_: str, external_id: str) -> Dict[str, Any]:
        return self._http.request("GET", f"/integration/mappings/{type_}/external/{_enc(external_id)}")

    def to_external(self, type_: str, id_: str) -> Dict[str, Any]:
        return self._http.request("GET", f"/integration/mappings/{type_}/internal/{_enc(id_)}")


class ObensClient:
    """``base_url`` is the core api, e.g. ``https://api.obens.so/v2/core``."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
        max_retries: int = 3,
        retry_delay: float = 0.5,
        session=None,
        account_id: Optional[str] = None,
    ):
        self._settings = dict(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=max_retries,
                              retry_delay=retry_delay, session=session)
        self._http = Http(base_url, api_key, timeout, max_retries, retry_delay, session, account_id)
        self.sub_actors = Resource(self._http, "/integration/sub-actors")
        self.facilities = Resource(self._http, "/integration/facilities")
        self.accounts = _Accounts(self._http)
        self.mappings = _Mappings(self._http)

    def for_account(self, account_id: str) -> "ObensClient":
        """A client acting as an account delegated to the key's account (see ``accounts.accessible()``): farmers,
        facilities, links and id tables are then those of that account. Its ids are separate from the key account's."""
        return ObensClient(**self._settings, account_id=account_id)

    def whoami(self) -> Dict[str, Any]:
        """Who the key is: keyId, keyName, accountId, workspaceId. Handy as a connection check."""
        return self._http.request("GET", "/integration/whoami")

    def elements(self) -> List[Dict[str, Any]]:
        """The lists farmers can be created in: id, name, path."""
        return self._http.request("GET", "/integration/elements")

    def sub_actor_schema(self, element_id: str) -> List[Dict[str, Any]]:
        """Custom fields of the farmers in a list. Use a field's name (or staticId) as a key of `data`."""
        return self._http.request("GET", "/integration/sub-actors/schema?element-id=" + quote(element_id, safe=""))

    def id_map(self, type_: str) -> IdMap:
        """Id table for ``SUB_ACTOR`` | ``FACILITY`` | ``ACCOUNT``. Call ``.load()`` to fill it."""
        return IdMap(self, type_)

    def syncer(
        self,
        kind: str,
        map: Callable[[Any], Dict[str, Any]],  # noqa: A002
        chunk_size: int = 200,
        on_progress: Optional[Callable] = None,
    ) -> Syncer:
        """Initial load + push-on-change for ``"sub_actors"`` or ``"facilities"``."""
        if kind not in ("sub_actors", "facilities"):
            raise ValueError("kind must be 'sub_actors' or 'facilities'")
        return Syncer(getattr(self, kind), kind, map, chunk_size, on_progress)

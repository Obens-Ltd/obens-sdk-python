from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

from .http import Http
from .idmap import IdMap
from .sync import MAX_BULK, Syncer


def _enc(value: str) -> str:
    return quote(str(value), safe="")


def _qs(**pairs: Any) -> str:
    """``?a=1&b=2`` from the given pairs, skipping the unset ones (underscores become dashes)."""
    parts = [f"{k.replace('_', '-')}={_enc(v)}" for k, v in pairs.items() if v is not None and v != ""]
    return "?" + "&".join(parts) if parts else ""


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


class _TraceResource:
    """Batches and transactions, which live in the trace api (a second base url, the workspace id as a header)."""

    def __init__(self, client: "ObensClient", path: str, kind: str):
        self._client = client
        self._path = path
        self._kind = kind

    def upsert(self, push: Dict[str, Any]) -> Dict[str, Any]:
        """Creates the record the first time ``externalId`` is seen, updates it afterwards. Safe to repeat.
        Creation waits for the platform to finish, so the call can take several seconds."""
        if not push.get("externalId"):
            raise ValueError("externalId is required")
        body = self._client._resolve(push, self._kind)
        external_id = body.pop("externalId")
        return self._client._trace.request("PUT", f"{self._path}/{_enc(external_id)}", body)

    def bulk(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Upserts many, one after the other (each creation is a heavy write). One result per item;
        a failure does not stop the rest."""
        results: List[Dict[str, Any]] = []
        for item in items:
            try:
                results.append(self.upsert(item))
            except Exception as error:  # noqa: BLE001 - reported per item, like the server's bulk answers
                results.append({"externalId": item.get("externalId"), "id": None, "outcome": "FAILED", "error": str(error)})
        return results


class _Batches(_TraceResource):
    def __init__(self, client: "ObensClient"):
        super().__init__(client, "/integration/batches", "batch")

    def get(self, external_id: str, owner_id: Optional[str] = None) -> Dict[str, Any]:
        """``owner_id``: a sub-actor's platform id, for what a purchase from a farmer created."""
        query = f"?owner-id={_enc(owner_id)}" if owner_id else ""
        return self._client._trace.request("GET", f"{self._path}/{_enc(external_id)}{query}")

    def list(self, element_id: str, owner_id: Optional[str] = None, search: Optional[str] = None,
             year: Optional[int] = None, page: Optional[int] = None, size: Optional[int] = None) -> Dict[str, Any]:
        """A page of batches in an element: data, page, size, total, totalPages, hasNext. ``search`` matches name / ref."""
        query = _qs(element_id=element_id, owner_id=owner_id, search=search, year=year, page=page, size=size)
        return self._client._trace.request("GET", f"{self._path}{query}")

    def data(self, external_id: str, owner_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """What the batch's form holds: id, staticId, name, type, value (in the shape ``data`` accepts)."""
        return self._client._trace.request("GET", f"{self._path}/{_enc(external_id)}/data{_qs(owner_id=owner_id)}")

    def schema(self, production_method_id: str) -> List[Dict[str, Any]]:
        """The fields of a production method's form: the names ``data`` accepts."""
        return self._client._trace.request("GET", f"{self._path}/schema?production-method-id={_enc(production_method_id)}")


class _Transactions(_TraceResource):
    def __init__(self, client: "ObensClient"):
        super().__init__(client, "/integration/transactions", "transaction")

    def get(self, external_id: str, supplier_id: Optional[str] = None) -> Dict[str, Any]:
        """``supplier_id``: a sub-actor's platform id, for a purchase from a farmer (default: the key's account)."""
        query = f"?supplier-id={_enc(supplier_id)}" if supplier_id else ""
        return self._client._trace.request("GET", f"{self._path}/{_enc(external_id)}{query}")

    def list(self, element_id: str, other_account_id: Optional[str] = None, type: Optional[str] = None,  # noqa: A002
             start: Optional[str] = None, end: Optional[str] = None, search: Optional[str] = None,
             page: Optional[int] = None, size: Optional[int] = None) -> Dict[str, Any]:
        """A page of the account's transactions in an element, as buyer or seller. ``type``: BUY | SELL;
        ``start`` / ``end``: 2026-01-31; ``other_account_id``: the counterparty's platform id."""
        query = _qs(element_id=element_id, other_account_id=other_account_id, type=type, start=start, end=end,
                    search=search, page=page, size=size)
        return self._client._trace.request("GET", f"{self._path}{query}")

    def data(self, external_id: str, supplier_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """What the transaction's form holds: id, staticId, name, type, value."""
        return self._client._trace.request("GET", f"{self._path}/{_enc(external_id)}/data{_qs(supplier_id=supplier_id)}")

    def schema(self, element_id: str, client_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """The fields of the transaction form an account (the buyer, default: the key's) has for an element."""
        query = f"?element-id={_enc(element_id)}" + (f"&client-id={_enc(client_id)}" if client_id else "")
        return self._client._trace.request("GET", f"{self._path}/schema{query}")


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
    """``base_url`` is the core api, e.g. ``https://api.obens.so/v2/core``. ``trace_url`` is the trace api that holds
    batches and transactions; it defaults to ``base_url`` with ``/v2/core`` swapped for ``/v2/trace``."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
        max_retries: int = 3,
        retry_delay: float = 0.5,
        session=None,
        account_id: Optional[str] = None,
        trace_url: Optional[str] = None,
        trace_timeout: float = 90.0,
    ):
        self._settings = dict(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=max_retries,
                              retry_delay=retry_delay, session=session, trace_url=trace_url, trace_timeout=trace_timeout)
        self._http = Http(base_url, api_key, timeout, max_retries, retry_delay, session, account_id)
        self._workspace_id: Optional[str] = None
        self._ids: Dict[str, str] = {}
        # creations are heavy and the server waits for them (up to a minute): a longer timeout than for farmers
        self._trace = Http(trace_url or re.sub(r"/v2/core/*$", "/v2/trace", base_url), api_key, trace_timeout,
                           max_retries, retry_delay, session, account_id,
                           extra_headers=lambda: {"X-WORKSPACE-ID": self._workspace()})
        self.batches = _Batches(self)
        self.transactions = _Transactions(self)
        self.sub_actors = Resource(self._http, "/integration/sub-actors")
        self.facilities = Resource(self._http, "/integration/facilities")
        self.accounts = _Accounts(self._http)
        self.mappings = _Mappings(self._http)

    def for_account(self, account_id: str) -> "ObensClient":
        """A client acting as an account delegated to the key's account (see ``accounts.accessible()``): farmers,
        facilities, links and id tables are then those of that account. Its ids are separate from the key account's."""
        return ObensClient(**self._settings, account_id=account_id)

    def _workspace(self) -> str:
        # the key's workspace: the trace api is routed by it, and core knows it from the key
        if not self._workspace_id:
            self._workspace_id = self.whoami()["workspaceId"]
        return self._workspace_id

    def _internal(self, type_: str, external_id: str) -> str:
        key = f"{type_}:{external_id}"
        if key not in self._ids:
            self._ids[key] = self.mappings.to_internal(type_, external_id)["id"]
        return self._ids[key]

    def _resolve(self, push: Dict[str, Any], kind: str) -> Dict[str, Any]:
        """Replaces your own ids in a transaction with platform ids: ``subActorExternalId`` -> ``subActorId``,
        ``supplierExternalId`` / ``clientExternalId`` -> ``supplierId`` / ``clientId`` (linked accounts) and
        ``collected.facilityExternalIds`` -> ``collected.facilityIds``."""
        out = dict(push)
        if kind != "transaction":
            return out
        if "subActorExternalId" in out:
            out["subActorId"] = self._internal("SUB_ACTOR", out.pop("subActorExternalId"))
        for role in ("supplier", "client"):
            if f"{role}ExternalId" in out:
                out[f"{role}Id"] = self._internal("ACCOUNT", out.pop(f"{role}ExternalId"))
        collected = out.get("collected")
        if collected and "facilityExternalIds" in collected:
            collected = dict(collected)
            collected["facilityIds"] = [self._internal("FACILITY", f) for f in collected.pop("facilityExternalIds")]
            out["collected"] = collected
        return out

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

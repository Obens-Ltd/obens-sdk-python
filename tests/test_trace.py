import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote

from obens_sdk import ObensClient


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, body=None):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            payload = b"" if body is None else json.dumps(body).encode()
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _handle(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            route = self.path.split("?")[0]
            state["calls"].append({"method": self.command, "url": self.path,
                                   "workspace": self.headers.get("X-WORKSPACE-ID"), "body": body})
            if route == "/integration/whoami":
                state["whoami"] += 1
                return self._send(200, {"keyId": "k", "keyName": "n", "accountId": "a1", "workspaceId": "w1"})
            mappings = {
                "/integration/mappings/SUB_ACTOR/external/farmer-1": ("SUB_ACTOR", "farmer-1", "sa-1"),
                "/integration/mappings/FACILITY/external/orchard-1": ("FACILITY", "orchard-1", "fa-1"),
                "/integration/mappings/ACCOUNT/external/buyer-1": ("ACCOUNT", "buyer-1", "ac-9"),
            }
            if route in mappings:
                t, e, i = mappings[route]
                return self._send(200, {"type": t, "externalId": e, "id": i})
            if self.command == "PUT" and route.startswith("/integration/batches/"):
                if body.get("name") == "bad":
                    return self._send(400, {"message": "QUANTITY_IS_REQUIRED"})
                return self._send(200, {"externalId": unquote(route.split("/")[-1]), "id": "b-1", "outcome": "CREATED", "error": None})
            if self.command == "PUT" and route.startswith("/integration/transactions/"):
                return self._send(200, {"externalId": unquote(route.split("/")[-1]), "id": "t-1", "outcome": "CREATED", "error": None})
            if route == "/integration/batches":
                return self._send(200, {"data": [{"id": "b-1", "externalId": "lot-1"}], "page": 0, "size": 10, "total": 1})
            if route == "/integration/transactions":
                return self._send(200, {"data": [{"id": "t-1", "externalId": "tr-1"}], "page": 0, "size": 10, "total": 1})
            if route.endswith("/data"):
                return self._send(200, [{"id": "f1", "name": "Moisture", "type": "NumericInput", "value": 12}])
            if route == "/integration/batches/schema":
                return self._send(200, [{"id": "f1", "name": "Moisture", "type": "NumericInput", "writable": True}])
            self._send(404)

        do_GET = do_PUT = do_POST = do_DELETE = _handle

    return Handler


class TraceTest(unittest.TestCase):
    def setUp(self):
        self.state = {"calls": [], "whoami": 0}
        self.server = HTTPServer(("127.0.0.1", 0), make_handler(self.state))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.client = ObensClient(self.url, "obk_good", trace_url=self.url, max_retries=0)

    def tearDown(self):
        self.server.shutdown()

    def puts(self):
        return [c for c in self.state["calls"] if c["method"] == "PUT"]

    def test_batch_push_goes_to_trace_with_the_workspace_asked_once(self):
        r = self.client.batches.upsert({"externalId": "lot/1", "name": "Lot", "quantity": {"value": 5, "unit": "KG"},
                                        "data": {"Moisture": 12}})
        self.client.batches.upsert({"externalId": "lot/1", "name": "Lot"})
        self.assertEqual(r["outcome"], "CREATED")
        first = self.puts()[0]
        self.assertEqual(first["url"], "/integration/batches/lot%2F1")
        self.assertEqual(first["workspace"], "w1")
        self.assertEqual(first["body"], {"name": "Lot", "quantity": {"value": 5, "unit": "KG"}, "data": {"Moisture": 12}})
        self.assertEqual(self.state["whoami"], 1)

    def test_purchase_names_farmer_buyer_and_facilities_by_client_ids(self):
        self.client.transactions.upsert({
            "externalId": "buy-1", "elementId": "e1", "subActorExternalId": "farmer-1", "date": "2026-02-01",
            "collected": {"name": "Olives", "quantity": {"value": 100, "unit": "KG"}, "facilityExternalIds": ["orchard-1"]},
        })
        self.client.transactions.upsert({"externalId": "tr-1", "elementId": "e1", "clientExternalId": "buyer-1",
                                         "supplierId": "a1", "batches": []})
        purchase, transfer = [c["body"] for c in self.puts()]
        self.assertEqual(purchase["subActorId"], "sa-1")
        self.assertNotIn("subActorExternalId", purchase)
        self.assertEqual(purchase["collected"]["facilityIds"], ["fa-1"])
        self.assertEqual(transfer["clientId"], "ac-9")
        self.assertEqual(transfer["supplierId"], "a1")

    def test_bulk_keeps_going_after_a_failed_item(self):
        results = self.client.batches.bulk([{"externalId": "a", "name": "ok"}, {"externalId": "b", "name": "bad"},
                                            {"externalId": "c", "name": "ok"}])
        self.assertEqual([r["outcome"] for r in results], ["CREATED", "FAILED", "CREATED"])
        self.assertIn("QUANTITY_IS_REQUIRED", results[1]["error"])

    def test_trace_url_defaults_from_core_url_and_reads(self):
        c = ObensClient("https://api.obens.so/v2/core", "obk_x")
        self.assertEqual(c._trace.base_url, "https://api.obens.so/v2/trace")
        self.assertEqual(self.client.batches.schema("pm1")[0]["name"], "Moisture")
        self.assertEqual(self.client.batches.list("e1", owner_id="sa-1")["data"][0]["externalId"], "lot-1")
        self.assertTrue(any(c["url"] == "/integration/batches?element-id=e1&owner-id=sa-1" for c in self.state["calls"]))

    def test_reads_build_their_query_and_read_form_data(self):
        page = self.client.transactions.list("e1", type="BUY", start="2026-01-01", search="olive oil", size=5)
        self.assertEqual(page["data"][0]["externalId"], "tr-1")
        urls = [c["url"] for c in self.state["calls"]]
        self.assertIn("/integration/transactions?element-id=e1&type=BUY&start=2026-01-01&search=olive%20oil&size=5", urls)
        self.assertEqual(self.client.batches.data("lot/1", owner_id="sa-1")[0]["value"], 12)
        self.assertIn("/integration/batches/lot%2F1/data?owner-id=sa-1", [c["url"] for c in self.state["calls"]])
        self.assertEqual(self.client.transactions.data("tr-1")[0]["name"], "Moisture")


if __name__ == "__main__":
    unittest.main()

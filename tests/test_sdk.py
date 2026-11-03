import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote

from obens_sdk import ObensApiError, ObensClient, constant, create_mapper


class Fake:
    def __init__(self):
        self.calls = []
        self.sub_actors = {}
        self.fail_first = 0


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, body=None, headers=None):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            payload = b"" if body is None else json.dumps(body).encode()
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _upsert(self, item):
            created = item["externalId"] not in state.sub_actors
            if created:
                state.sub_actors[item["externalId"]] = f"id-{len(state.sub_actors) + 1}"
            return {"externalId": item["externalId"], "id": state.sub_actors[item["externalId"]],
                    "outcome": "CREATED" if created else "UPDATED", "error": None}

        def _handle(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            route = self.path.split("?")[0]
            state.calls.append({"method": self.command, "url": self.path, "key": self.headers.get("X-API-KEY"), "body": body})
            if self.headers.get("X-API-KEY") != "obk_good":
                return self._send(401)
            if state.fail_first > 0:
                state.fail_first -= 1
                return self._send(503, {}, {"Retry-After": "0"})
            if self.command == "PUT" and route.startswith("/integration/sub-actors/"):
                return self._send(200, self._upsert({**body, "externalId": unquote(route.split("/")[-1])}))
            if self.command == "POST" and route == "/integration/sub-actors/bulk":
                return self._send(200, [
                    {"externalId": i["externalId"], "id": None, "outcome": "FAILED", "error": "nope"}
                    if i.get("name") == "bad" else self._upsert(i) for i in body["items"]])
            if route == "/integration/sub-actors/schema":
                return self._send(200, [{"id": "f1", "name": "Crop", "type": "TextInput", "writable": True}])
            if route == "/integration/mappings/SUB_ACTOR":
                return self._send(200, [{"type": "SUB_ACTOR", "externalId": k, "id": v} for k, v in state.sub_actors.items()])
            if route.startswith("/integration/mappings/SUB_ACTOR/external/"):
                ext = unquote(route.split("/")[-1])
                if ext in state.sub_actors:
                    return self._send(200, {"type": "SUB_ACTOR", "externalId": ext, "id": state.sub_actors[ext]})
                return self._send(404, {"message": "x"})
            self._send(404)

        do_GET = do_PUT = do_POST = do_DELETE = _handle

    return Handler


class SdkTest(unittest.TestCase):
    def setUp(self):
        self.state = Fake()
        self.server = HTTPServer(("127.0.0.1", 0), make_handler(self.state))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def client(self, key="obk_good"):
        return ObensClient(self.url, key, retry_delay=0.001)

    def test_mapper_paths_functions_constants_and_empty_values(self):
        mapper = create_mapper({
            "externalId": "id",
            "name": lambda r: f"{r['first']} {r['last']}",
            "info.name": "site.title",
            "type": constant("FARMER"),
            "ref": "missing",
        })
        self.assertEqual(
            mapper({"id": 7, "first": "A", "last": "B", "site": {"title": "T"}}),
            {"externalId": 7, "name": "A B", "info": {"name": "T"}, "type": "FARMER"},
        )

    def test_upsert_sends_key_and_encodes_external_id(self):
        result = self.client().sub_actors.upsert({"externalId": "a/b c", "name": "X"})
        self.assertEqual(result["outcome"], "CREATED")
        self.assertEqual(self.state.calls[0]["url"], "/integration/sub-actors/a%2Fb%20c")
        self.assertEqual(self.state.calls[0]["key"], "obk_good")

    def test_sub_actor_schema_asks_for_the_custom_fields(self):
        fields = self.client().sub_actor_schema("el 1")
        self.assertEqual(fields[0]["name"], "Crop")
        self.assertIn("schema?element-id=el%201", self.state.calls[-1]["url"])

    def test_wrong_key_is_401_without_retries(self):
        with self.assertRaises(ObensApiError) as ctx:
            self.client("nope").whoami()
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(len(self.state.calls), 1)

    def test_transient_503_is_retried(self):
        self.state.fail_first = 2
        self.assertEqual(self.client().sub_actors.upsert({"externalId": "1", "name": "X"})["outcome"], "CREATED")
        self.assertEqual(len(self.state.calls), 3)

    def test_init_chunks_reports_and_replay_updates(self):
        rows = [{"id": f"f{i}", "name": "bad" if i == 3 else f"n{i}"} for i in range(5)]
        farmers = self.client().syncer("sub_actors", map=create_mapper({"externalId": "id", "name": "name"}), chunk_size=2)
        first = farmers.init(rows)
        self.assertEqual(len([c for c in self.state.calls if c["url"].endswith("/bulk")]), 3)
        self.assertEqual((first.total, first.created, first.failed), (5, 4, 1))
        self.assertEqual(first.failures[0]["externalId"], "f3")
        replay = farmers.init(r for r in rows if r["name"] != "bad")
        self.assertEqual((replay.created, replay.updated), (0, 4))

    def test_remove_sends_deleted_status(self):
        self.client().syncer("sub_actors", map=create_mapper({"externalId": "id"})).remove({"id": "z"})
        self.assertEqual(self.state.calls[0]["body"]["status"], "DELETED")

    def test_id_map_translates_both_ways_and_404_is_unknown(self):
        client = self.client()
        client.sub_actors.upsert({"externalId": "A", "name": "x"})
        ids = client.id_map("SUB_ACTOR").load()
        self.assertEqual(ids.internal("A"), "id-1")
        self.assertEqual(ids.external("id-1"), "A")
        self.assertIsNone(ids.internal("missing"))

    def test_for_account_scopes_calls_to_a_delegated_account(self):
        client = self.client()
        delegated = client.for_account("acc 1")
        delegated.sub_actors.upsert({"externalId": "1", "name": "X"})
        client.sub_actors.upsert({"externalId": "2", "name": "Y"})
        delegated.id_map("SUB_ACTOR").load()
        self.assertEqual(self.state.calls[0]["url"], "/integration/sub-actors/1?account-id=acc%201")
        self.assertEqual(self.state.calls[1]["url"], "/integration/sub-actors/2")
        self.assertEqual(self.state.calls[2]["url"], "/integration/mappings/SUB_ACTOR?account-id=acc%201")


if __name__ == "__main__":
    unittest.main()

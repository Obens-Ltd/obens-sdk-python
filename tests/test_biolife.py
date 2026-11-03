"""Biolife (e-pineA) pushes `{ type, action, data }` events from its field app. This shows how those records go
through the SDK: the producer and the field become farmers and facilities, keyed by the app's own `appId`."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote

from obens_sdk import ObensClient, constant, create_mapper

FARMERS_ELEMENT = "66f000000000000000000001"


def producteur(n, **extra):
    data = {
        "appId": f"prd-{n}", "firstName": "Afi", "lastName": "Houngbo", "phone": f"+2299700000{n}", "gender": "F",
        "birthday": "1985-04-12", "zone": "ZON_3", "address": "Allada, Sékou",
    }
    data.update(extra)
    return {"type": "producteur", "action": "create", "data": data}


def champ(n, producteur_id, **extra):
    data = {
        "appId": f"chp-{n}", "producteurId": producteur_id, "libelle": f"Champ {n}", "plantingDate": "2025-11-03",
        "latitude": 6.6512, "longitude": 2.1534, "varietyId": "VAR_1", "zoneId": "ZON_3",
        "coordinates": [{"lat": 6.65, "lng": 2.15}, {"lat": 6.65, "lng": 2.16}, {"lat": 6.66, "lng": 2.16}],
    }
    data.update(extra)
    return {"type": "fields", "action": "create", "data": data}


def ring(points):
    r = [[p["lng"], p["lat"]] for p in points]  # GeoJSON order is [longitude, latitude]
    return r + [r[0]]                           # and the ring is closed


def field_coordinates(event):
    data = event["data"]
    if len(data.get("coordinates") or []) >= 3:
        return {"type__": "Area", "type": "FeatureCollection",
                "features": [{"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring(data["coordinates"])]}}]}
    return {"type__": "Point", "latitude": data["latitude"], "longitude": data["longitude"]}


map_field = create_mapper({
    "externalId": "data.appId",
    "info.name": lambda e: e["data"]["libelle"],
    "info.description": lambda e: f"Planté le {e['data']['plantingDate']}",
    "coordinates": field_coordinates,
})

map_producer = create_mapper({
    "externalId": "data.appId",                    # the app's own code: sending it twice never duplicates
    "name": lambda e: f"{e['data']['firstName']} {e['data']['lastName']}",
    "ref": "data.phone",                           # the phone is what identifies a producer for them
    "address": "data.address",
    "gender": lambda e: {"M": "MALE", "F": "FEMALE"}.get(e["data"]["gender"]),
    "type": constant("FARMER"),
    "elementId": constant(FARMERS_ELEMENT),
})


class State:
    def __init__(self):
        self.bodies = []
        self.known = set()


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _upsert(self, item):
            state.bodies.append(item)
            created = item["externalId"] not in state.known
            state.known.add(item["externalId"])
            return {"externalId": item["externalId"], "id": "id-" + item["externalId"],
                    "outcome": "CREATED" if created else "UPDATED", "error": None}

        def _handle(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            if self.command == "PUT":
                out = self._upsert({**body, "externalId": unquote(self.path.split("?")[0].split("/")[-1])})
            else:
                out = [self._upsert(i) for i in body["items"]]
            payload = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_PUT = do_POST = _handle

    return Handler


class BiolifeTest(unittest.TestCase):
    def setUp(self):
        self.state = State()
        self.server = HTTPServer(("127.0.0.1", 0), make_handler(self.state))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.client = ObensClient(f"http://127.0.0.1:{self.server.server_port}", "obk_good", retry_delay=0.001)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_producers_with_the_same_name_stay_two_farmers_and_a_replay_updates(self):
        farmers = self.client.syncer("sub_actors", map=map_producer)
        events = [producteur(1), producteur(2), producteur(3, gender="X")]

        first = farmers.init(events)
        self.assertEqual((first.total, first.created, first.failed), (3, 3, 0))
        self.assertEqual(self.state.bodies[0], {
            "externalId": "prd-1", "name": "Afi Houngbo", "ref": "+22997000001", "address": "Allada, Sékou",
            "gender": "FEMALE", "type": "FARMER", "elementId": FARMERS_ELEMENT,
        })
        self.assertEqual(self.state.bodies[0]["name"], self.state.bodies[1]["name"])
        self.assertNotEqual(self.state.bodies[0]["ref"], self.state.bodies[1]["ref"])
        self.assertNotIn("gender", self.state.bodies[2], "an unknown gender is left out, not sent as null")

        replay = farmers.init(events)
        self.assertEqual((replay.created, replay.updated), (0, 3))

    def test_a_traced_field_becomes_a_closed_polygon_an_untraced_one_a_point(self):
        facilities = self.client.syncer("facilities", map=map_field)

        report = facilities.init([champ(1, "prd-1"), champ(2, "prd-1", coordinates=[{"lat": 6.6, "lng": 2.1}])])
        self.assertEqual((report.created, report.failed), (2, 0))

        traced, untraced = self.state.bodies
        self.assertEqual(traced["info"]["name"], "Champ 1")
        polygon = traced["coordinates"]["features"][0]["geometry"]
        self.assertEqual(polygon["type"], "Polygon")
        self.assertEqual(polygon["coordinates"][0][0], [2.15, 6.65])
        self.assertEqual(polygon["coordinates"][0][-1], polygon["coordinates"][0][0])
        self.assertEqual(len(polygon["coordinates"][0]), 4)
        self.assertEqual(untraced["coordinates"], {"type__": "Point", "latitude": 6.6512, "longitude": 2.1534})

    def test_create_update_push_delete_removes_and_other_record_types_are_not_ours(self):
        farmers = self.client.syncer("sub_actors", map=map_producer)
        facilities = self.client.syncer("facilities", map=map_field)

        def handle(event):  # what the integration does with the stream the app sends
            target = {"producteur": farmers, "fields": facilities}.get(event["type"])
            if target is None:
                return None  # tif, phytosanitaire, conditionnement have no counterpart yet
            return target.remove(event) if event["action"] == "delete" else target.push(event)

        self.assertEqual(handle(producteur(1))["outcome"], "CREATED")
        update = producteur(1)
        update.update(action="update", data={**update["data"], "lastName": "Houngbo-Dossou"})
        self.assertEqual(handle(update)["outcome"], "UPDATED")
        self.assertEqual(self.state.bodies[-1]["name"], "Afi Houngbo-Dossou")

        handle({**producteur(1), "action": "delete"})
        self.assertEqual(self.state.bodies[-1]["status"], "DELETED")
        handle({**champ(1, "prd-1"), "action": "delete"})
        self.assertIs(self.state.bodies[-1]["deleted"], True)

        before = len(self.state.bodies)
        self.assertIsNone(handle({"type": "tif", "action": "create", "data": {"appId": "tif-1"}}))
        self.assertEqual(len(self.state.bodies), before)


if __name__ == "__main__":
    unittest.main()

"""Generated HTTP/preflight sequences; these guarantees do not apply to all final refusals."""

from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from app import create_app
from app.services.copies import CopyService
from app.services.personal import PersonalCollectionService

from .harness import NOW, scenario_fixture
from .metrics import record

ACTION = st.tuples(st.integers(0, 7), st.integers(0, 2))


@settings(max_examples=25, deadline=None)
@given(st.lists(ACTION, min_size=14, max_size=14))
def test_http_preflight_failures_and_successful_writes(actions):
    actions = [(0, 0), (1, 1), (2, 2), (3, 2), (6, 2), (7, 1)] + actions
    with scenario_fixture() as fixture:
        clock = [NOW]
        staff, owner = fixture.actors[2], fixture.actors[0]
        entry = PersonalCollectionService(fixture.backend).create(
            owner["token"],
            {"movie_id": fixture.movie["id"], "kind": "custom", "list_name": "Initial"},
            timestamp=NOW,
        )
        item = CopyService(fixture.backend).create(
            staff["token"],
            {
                "movie_id": fixture.movie["id"],
                "support": "dvd",
                "condition": "good",
                "location": "Initial",
            },
            timestamp=NOW,
        )
        app = create_app(
            {
                "TESTING": True,
                "ENTERPRISE_ENABLED": True,
                "ENTERPRISE_DATA_FILE": str(fixture.path),
                "DATA_FILE": str(fixture.path.parent / "legacy.json"),
                "AUTH_CLOCK": lambda: clock[0],
                "AUTH_THROTTLE_KEY": b"synthetic-property-fixture-key!!xx",
            }
        )
        client = app.test_client()
        entities = [fixture.movie["id"], item.body["id"], entry.body["id"]]
        kinds = ("movies", "copies", "collections")
        for step, (operation, domain) in enumerate(actions, start=1):
            clock[0] = NOW + step
            actor = owner if domain == 2 else staff
            auth = {"Authorization": "Bearer " + actor["token"]}
            endpoint = "/api/v1/" + kinds[domain]
            identity = entities[domain]
            before = fixture.path.read_bytes()
            revision = fixture.backend.query(lambda work: work.revision)
            if operation == 0:
                response = client.post(
                    endpoint, headers=auth, data="{", content_type="application/json"
                )
                expected = 400
            elif operation == 1:
                invalid = (
                    {
                        "title": "",
                        "year": 2026,
                        "director": "A",
                        "actors": [],
                        "genres": [],
                        "synopsis": "",
                    },
                    {
                        "movie_id": fixture.movie["id"],
                        "support": "dvd",
                        "condition": "invalid",
                        "location": "",
                    },
                    {"movie_id": fixture.movie["id"], "kind": "custom", "list_name": ""},
                )[domain]
                response = client.post(endpoint, headers=auth, json=invalid)
                expected = 422
            elif operation in (2, 3, 4, 5):
                headers = auth.copy()
                if operation == 3:
                    headers["If-Match"] = '"obsolete-property"'
                if operation == 4:
                    headers["Authorization"] = "Bearer invalid"
                if operation == 5:
                    # Foreign admin still cannot see the owner's private entry;
                    # catalogue member refusal takes precedence over broken JSON.
                    foreign = staff if domain == 2 else owner
                    headers["Authorization"] = "Bearer " + foreign["token"]
                response = client.put(
                    endpoint + "/" + identity,
                    headers=headers,
                    data="{",
                    content_type="application/json",
                )
                expected = {2: 428, 3: 412, 4: 401, 5: 404 if domain == 2 else 403}[operation]
            elif operation == 6:
                # A successful creation is one final write, even after earlier refusals.
                body = {
                    "movie_id": fixture.movie["id"],
                    "kind": "custom",
                    "list_name": "New" + str(step),
                }
                with patch.object(
                    app.extensions["enterprise_persistence"],
                    "run",
                    wraps=app.extensions["enterprise_persistence"].run,
                ) as commands:
                    response = client.post(
                        "/api/v1/collections",
                        headers={"Authorization": "Bearer " + owner["token"]},
                        json=body,
                    )
                assert response.status_code == 201 and commands.call_count == 1
                assert fixture.backend.query(lambda work: work.revision) == revision + 1
                assert fixture.session_state(owner)["last_seen"] == clock[0]
                assert response.headers["Location"].endswith("/" + response.get_json()["id"])
                continue
            else:
                # The HTTP adapter supplies Location on replay, absent in the pure service.
                body = {
                    "movie_id": fixture.movie["id"],
                    "support": "dvd",
                    "condition": "good",
                    "location": "Receipt",
                }
                headers = {
                    "Authorization": "Bearer " + staff["token"],
                    "Idempotency-Key": "property-http-key-" + str(step),
                }
                first = client.post("/api/v1/copies", headers=headers, json=body)
                replay = client.post("/api/v1/copies", headers=headers, json=body)
                assert first.status_code == replay.status_code == 201
                assert first.get_json() == replay.get_json()
                for header in ("ETag", "Location"):
                    assert first.headers[header] == replay.headers[header]
                assert replay.headers["Idempotency-Replayed"] == "true"
                continue
            assert response.status_code == expected
            assert fixture.path.read_bytes() == before, "HTTP/preflight refusal must not write"
            assert fixture.backend.query(lambda work: work.revision) == revision
        record("http_json", len(actions))

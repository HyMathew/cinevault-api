"""Behavioral contract for the explicit JSON unit of work and repositories."""

import copy
import importlib.util
import multiprocessing
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial

import pytest

from app.storage_v2 import V2Store


def implementation():
    assert importlib.util.find_spec("app.persistence.json") is not None, (
        "JsonPersistence is not implemented"
    )
    from app.persistence.json import JsonPersistence

    return JsonPersistence


def identity():
    return str(uuid.uuid4())


def user(username="member", *, role="member", enabled=True):
    return {
        "id": identity(),
        "username": username,
        "display_name": username,
        "password_hash": "$argon2id$fixture",
        "role": role,
        "enabled": enabled,
        "created_at": 0,
    }


def movie(title="Étoile"):
    return {
        "id": identity(),
        "title": title,
        "year": 2026,
        "director": "Auteur",
        "actors": ["Interprète"],
        "synopsis": "",
        "genres": ["Drame"],
    }


def film_copy(movie_id):
    return {
        "id": identity(),
        "movie_id": movie_id,
        "support": "dvd",
        "location": "Rayon A",
        "condition": "good",
    }


def session(user_id, *, created_at=0, last_seen=0, expires_at=100):
    return {
        "id": identity(),
        "user_id": user_id,
        "token_hash": uuid.uuid4().hex + uuid.uuid4().hex,
        "created_at": created_at,
        "last_seen": last_seen,
        "expires_at": expires_at,
    }


@pytest.fixture
def persistence(tmp_path):
    path = tmp_path / "enterprise.json"
    V2Store(path).initialize()
    return implementation()(path), path


def test_command_commits_one_revision_and_read_query_never_writes(tmp_path):
    path = tmp_path / "enterprise.json"
    V2Store(path).initialize()
    backend = implementation()(path)
    first, film = user(), movie()

    def create(work):
        assert work.revision == 1
        work.accounts.insert(first)
        work.catalogue.insert_movie(film)
        return work.catalogue.movie(film["id"])

    assert backend.run(create) == film
    stored = V2Store(path).read()
    assert stored["revision"] == 1
    assert stored["users"] == [first]
    assert stored["movies"] == [film]
    before = path.read_bytes()
    assert backend.query(lambda work: work.revision) == 1
    assert backend.query(lambda work: work.accounts.get(first["id"])) == first
    assert backend.readiness() is None
    assert path.read_bytes() == before


@pytest.mark.parametrize("failure", ["exception", "invalid_document", "cross_type_id"])
def test_command_aborts_without_partial_entities_or_revision(persistence, failure):
    backend, path = persistence
    first = user()
    before = path.read_bytes()

    def fail(work):
        work.accounts.insert(first)
        if failure == "exception":
            raise RuntimeError("abort")
        film = movie()
        if failure == "invalid_document":
            film["year"] = True
        else:
            film["id"] = first["id"]
        work.catalogue.insert_movie(film)

    with pytest.raises((RuntimeError, ValueError)):
        backend.run(fail)
    assert path.read_bytes() == before
    assert backend.run(lambda work: work.revision) == 1


def test_revision_has_no_public_setter_and_cannot_be_committed_twice(persistence):
    backend, path = persistence

    def change(work):
        with pytest.raises(AttributeError):
            work.revision = 999
        return work.revision

    assert backend.run(change) == 1
    assert backend.run(change) == 2
    assert V2Store(path).read()["revision"] == 2


@pytest.mark.parametrize("mode", ["run", "query"])
def test_work_and_retained_repositories_close_even_when_callback_raises(persistence, mode):
    backend, _ = persistence
    retained = []

    def callback(work):
        retained.extend([work, work.accounts, work.sessions, work.catalogue])
        raise RuntimeError("abort")

    with pytest.raises(RuntimeError, match="abort"):
        getattr(backend, mode)(callback)
    work, accounts, sessions, catalogue = retained
    for action in (
        lambda: work.revision,
        lambda: work.accounts,
        lambda: work.sessions,
        lambda: work.catalogue,
        lambda: accounts.page(10, 0),
        lambda: sessions.by_token_hash("a" * 64),
        lambda: catalogue.movie(identity()),
        lambda: accounts.insert(user()),
    ):
        with pytest.raises(RuntimeError, match="closed"):
            action()


def test_successful_callbacks_cannot_leak_active_work_or_repository(persistence):
    backend, _ = persistence
    work = backend.run(lambda current: current)
    repository = backend.query(lambda current: current.accounts)
    with pytest.raises(RuntimeError, match="closed"):
        _ = work.revision
    with pytest.raises(RuntimeError, match="closed"):
        repository.get(identity())


def test_all_query_writes_are_refused_and_input_entities_stay_detached(persistence):
    backend, path = persistence
    first, film = user(), movie()
    item = film_copy(film["id"])
    token = session(first["id"])

    def create(work):
        work.accounts.insert(first)
        work.sessions.insert(token)
        work.catalogue.insert_movie(film)
        work.catalogue.insert_copy(item)
        first["display_name"] = "Changed outside the repository"
        film["actors"].append("Not persisted")
        token["last_seen"] = 90
        item["location"] = "Not persisted"

    backend.run(create)
    before = path.read_bytes()

    def readonly(work):
        for write in (
            lambda: work.accounts.insert(user("other")),
            lambda: work.accounts.save(first),
            lambda: work.sessions.touch(token["id"], 1),
            lambda: work.sessions.insert(session(first["id"])),
            lambda: work.sessions.revoke(token["id"]),
            lambda: work.sessions.revoke_user(first["id"]),
            lambda: work.sessions.delete_expired(100, idle_ttl=30),
            lambda: work.catalogue.insert_movie(movie("Other")),
            lambda: work.catalogue.save_movie(film),
            lambda: work.catalogue.delete_movie(film["id"]),
            lambda: work.catalogue.insert_copy(film_copy(film["id"])),
            lambda: work.catalogue.save_copy(item),
            lambda: work.catalogue.delete_copy(item["id"]),
        ):
            with pytest.raises(RuntimeError, match="read-only"):
                write()
        work.accounts.get(first["id"])["display_name"] = "Detached"
        work.accounts.page(10, 0)[0]["display_name"] = "Detached"
        work.sessions.by_token_hash(token["token_hash"])["last_seen"] = 99
        work.catalogue.movie(film["id"])["actors"].clear()
        work.catalogue.copy(item["id"])["location"] = "Detached"
        work.catalogue.copies_for_movie(film["id"])[0]["location"] = "Detached"

    backend.query(readonly)
    assert path.read_bytes() == before
    stored = V2Store(path).read()
    assert stored["users"][0]["display_name"] == "member"
    assert stored["movies"][0]["actors"] == ["Interprète"]
    assert stored["sessions"][0]["last_seen"] == 0
    assert stored["copies"][0]["location"] == "Rayon A"


def test_mutating_a_read_inside_run_requires_explicit_save(persistence):
    backend, path = persistence
    first, film = user(), movie()
    backend.run(lambda work: (work.accounts.insert(first), work.catalogue.insert_movie(film)))

    def edit(work):
        work.accounts.get(first["id"])["display_name"] = "Discarded"
        work.catalogue.movie(film["id"])["genres"].clear()
        assert work.accounts.get(first["id"]) == first
        assert work.catalogue.movie(film["id"]) == film
        saved = work.accounts.get(first["id"])
        saved["display_name"] = "Explicit"
        work.accounts.save(saved)
        saved["display_name"] = "Discarded after save"

    backend.run(edit)
    assert V2Store(path).read()["users"][0]["display_name"] == "Explicit"
    assert backend.query(lambda work: work.catalogue.movie(film["id"])) == film


@pytest.mark.parametrize("outer", ["run", "query"])
@pytest.mark.parametrize("inner", ["run", "query", "readiness"])
def test_nested_calls_on_same_instance_fail_before_reacquiring_store_lock(
    persistence, outer, inner, monkeypatch
):
    backend, _ = persistence

    def nested(work):
        def forbidden_read(*args):
            pytest.fail("Nested UoW tried to acquire the store again")

        with monkeypatch.context() as patch:
            patch.setattr(V2Store, "read", forbidden_read)
            patch.setattr(V2Store, "transact", forbidden_read)
            with pytest.raises(RuntimeError, match="nested"):
                if inner == "readiness":
                    backend.readiness()
                else:
                    getattr(backend, inner)(lambda current: None)
        return work.revision

    assert getattr(backend, outer)(nested) == (1 if outer == "run" else 0)


@pytest.mark.parametrize(
    "bad", [{"$ne": None}, ["id"], None, "not-uuid", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"]
)
def test_uuid_selectors_are_rejected_before_any_lookup(persistence, bad):
    backend, path = persistence
    before = path.read_bytes()

    def probe(work):
        for read in (
            lambda: work.accounts.get(bad),
            lambda: work.accounts.has_other_enabled_admin(bad),
            lambda: work.catalogue.movie(bad),
            lambda: work.catalogue.copy(bad),
            lambda: work.catalogue.copies_for_movie(bad),
            lambda: work.catalogue.has_movie_references(bad),
            lambda: work.catalogue.has_copy_history(bad),
        ):
            with pytest.raises(ValueError):
                read()

    backend.query(probe)
    assert path.read_bytes() == before


@pytest.mark.parametrize("bad", [{"$regex": ".*"}, "Member", "member\n", None])
def test_username_and_token_selectors_are_typed_and_canonical(persistence, bad):
    backend, _ = persistence

    def probe(work):
        with pytest.raises(ValueError):
            work.accounts.by_username(bad)
        with pytest.raises(ValueError):
            work.sessions.by_token_hash(bad)

    backend.query(probe)


def test_account_reads_pagination_and_admin_predicate(persistence):
    backend, _ = persistence
    accounts = [user("zulu"), user("bravo", role="admin"), user("alpha", role="admin")]
    accounts.append(user("disabled", role="admin", enabled=False))
    backend.run(lambda work: [work.accounts.insert(account) for account in accounts])

    def read(work):
        assert work.accounts.get(identity()) is None
        assert work.accounts.by_username("missing") is None
        assert work.accounts.by_username("bravo") == accounts[1]
        assert work.accounts.page(2, 1) == [accounts[1], accounts[3]]
        assert work.accounts.has_other_enabled_admin(accounts[1]["id"])

    backend.query(read)
    changed = copy.deepcopy(accounts[2])
    changed["enabled"] = False
    backend.run(lambda work: work.accounts.save(changed))
    assert not backend.query(lambda work: work.accounts.has_other_enabled_admin(accounts[1]["id"]))
    for limit, offset in ((0, 0), (101, 0), (True, 0), (1, -1), (1, 10001), (1, False)):
        with pytest.raises(ValueError):
            backend.query(lambda work: work.accounts.page(limit, offset))


def test_sessions_touch_expire_and_revoke_without_deleting_other_accounts(persistence):
    backend, _ = persistence
    first, second = user(), user("other")
    active = session(first["id"], last_seen=90, expires_at=200)
    absolute = session(first["id"], last_seen=90, expires_at=100)
    idle = session(second["id"], last_seen=70, expires_at=200)
    future = session(second["id"], created_at=120, last_seen=120, expires_at=200)

    def create(work):
        work.accounts.insert(first)
        work.accounts.insert(second)
        for item in (active, absolute, idle, future):
            work.sessions.insert(item)
        work.sessions.touch(active["id"], 99)

    backend.run(create)
    assert (
        backend.query(lambda work: work.sessions.by_token_hash(active["token_hash"]))["last_seen"]
        == 99
    )
    assert backend.run(lambda work: work.sessions.delete_expired(100, idle_ttl=30)) == 2
    assert backend.query(lambda work: work.sessions.by_token_hash(idle["token_hash"])) is None
    assert backend.query(lambda work: work.sessions.by_token_hash(future["token_hash"])) == future
    assert backend.run(lambda work: work.sessions.revoke_user(first["id"])) == 1
    assert backend.run(lambda work: work.sessions.revoke(future["id"])) is True
    assert backend.run(lambda work: work.sessions.revoke(future["id"])) is False


def test_catalogue_crud_duplicate_matching_and_references_remain_explicit(persistence):
    backend, path = persistence
    film = movie("Straße")
    item = film_copy(film["id"])
    backend.run(lambda work: (work.catalogue.insert_movie(film), work.catalogue.insert_copy(item)))
    candidate = {**film, "title": "STRASSE", "director": "AUTEUR"}

    def inspect(work):
        assert work.catalogue.movie(identity()) is None
        assert work.catalogue.copy(identity()) is None
        assert work.catalogue.has_duplicate_movie(candidate)
        assert not work.catalogue.has_duplicate_movie(candidate, excluded_id=film["id"])
        assert work.catalogue.copies_for_movie(film["id"]) == [item]
        assert work.catalogue.has_movie_references(film["id"])
        assert not work.catalogue.has_copy_history(item["id"])

    backend.query(inspect)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        backend.run(lambda work: work.catalogue.delete_movie(film["id"]))
    assert path.read_bytes() == before
    film["synopsis"] = "Updated"
    item["location"] = "Rayon B"
    backend.run(lambda work: (work.catalogue.save_movie(film), work.catalogue.save_copy(item)))
    assert backend.query(lambda work: work.catalogue.movie(film["id"])) == film
    assert backend.query(lambda work: work.catalogue.copy(item["id"])) == item

    def remove(work):
        assert work.catalogue.delete_copy(item["id"])
        assert not work.catalogue.delete_copy(item["id"])
        assert work.catalogue.delete_movie(film["id"])
        assert not work.catalogue.delete_movie(film["id"])

    backend.run(remove)
    assert V2Store(path).read()["movies"] == []


def test_save_never_upserts_and_insert_never_overwrites(persistence):
    backend, path = persistence
    first, film = user(), movie()
    token, item = session(first["id"]), film_copy(film["id"])
    for write in (
        lambda work: work.accounts.save(first),
        lambda work: work.sessions.touch(token["id"], 1),
        lambda work: work.catalogue.save_movie(film),
        lambda work: work.catalogue.save_copy(item),
    ):
        before = path.read_bytes()
        with pytest.raises(LookupError):
            backend.run(write)
        assert path.read_bytes() == before
    backend.run(lambda work: work.accounts.insert(first))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        backend.run(lambda work: work.accounts.insert({**first, "display_name": "Overwrite"}))
    assert path.read_bytes() == before


def test_readiness_never_initializes_missing_or_repairs_corrupt_storage(tmp_path):
    path = tmp_path / "missing.json"
    backend = implementation()(path)
    with pytest.raises(FileNotFoundError):
        backend.readiness()
    assert not path.exists()
    path.write_text('{"schema_version":999}', encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError):
        backend.readiness()
    assert path.read_bytes() == before


@pytest.mark.parametrize("mode", ["run", "query"])
def test_work_cannot_be_used_concurrently_from_another_thread(persistence, mode):
    backend, _ = persistence

    def command(work):
        account_repository = work.accounts
        with ThreadPoolExecutor(max_workers=1) as pool:
            for access in (
                lambda: work.revision,
                lambda: account_repository.page(10, 0),
            ):
                future = pool.submit(access)
                with pytest.raises(RuntimeError, match="owner"):
                    future.result(timeout=5)
        assert work.accounts.page(10, 0) == []

    getattr(backend, mode)(command)


@pytest.mark.parametrize("mode", ["run", "query"])
@pytest.mark.parametrize("kind", ["async", "generator", "async-generator"])
def test_deferred_callback_is_rejected_before_storage_access(persistence, mode, kind, monkeypatch):
    backend, path = persistence
    before = path.read_bytes()

    async def asynchronous(work):
        pytest.fail("Deferred body must not execute")

    def generator(work):
        yield work.revision

    async def asynchronous_generator(work):
        yield work.revision

    command = {
        "async": asynchronous,
        "generator": generator,
        "async-generator": asynchronous_generator,
    }[kind]

    def forbidden(*args):
        pytest.fail("Deferred callback reached storage")

    with monkeypatch.context() as patch:
        patch.setattr(V2Store, "read", forbidden)
        patch.setattr(V2Store, "transact", forbidden)
        with pytest.raises(RuntimeError, match="synchronous"):
            getattr(backend, mode)(command)
    assert path.read_bytes() == before


@pytest.mark.parametrize("mode", ["run", "query"])
@pytest.mark.parametrize("kind", ["coroutine", "iterator", "future"])
def test_deferred_result_cannot_escape_closed_json_scope(persistence, mode, kind):
    backend, path = persistence
    before = path.read_bytes()

    async def value():
        return "not-executed"

    def callback(work):
        if mode == "run":
            work.accounts.insert(user())
        if kind == "coroutine":
            return value()
        return Future() if kind == "future" else iter([1, 2])

    with pytest.raises(RuntimeError, match="eager"):
        getattr(backend, mode)(callback)
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["method", "callable", "partial"])
def test_async_callable_forms_are_rejected_before_storage(persistence, kind, monkeypatch):
    backend, _ = persistence

    class Deferred:
        async def method(self, work):
            return "not-executed"

        async def __call__(self, work):
            return "not-executed"

    instance = Deferred()
    command = {
        "method": instance.method,
        "callable": instance,
        "partial": partial(instance.method),
    }[kind]

    def forbidden(*args):
        pytest.fail("Deferred callable reached storage")

    monkeypatch.setattr(V2Store, "transact", forbidden)
    with pytest.raises(RuntimeError, match="synchronous"):
        backend.run(command)


def add_account_in_process(path, username, queue):
    try:
        backend = implementation()(path)
        account = user(username)
        queue.put(backend.run(lambda work: (work.accounts.insert(account), work.revision)[1]))
    except Exception as exc:
        queue.put(type(exc).__name__)


def test_distinct_processes_commit_simple_actors_without_lost_updates(persistence):
    _, path = persistence
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(target=add_account_in_process, args=(str(path), f"actor{i}", queue))
        for i in range(4)
    ]
    try:
        for process in processes:
            process.start()
        outcomes = [queue.get(timeout=30) for _ in processes]
        for process in processes:
            process.join(timeout=30)
            assert process.exitcode == 0
        assert sorted(outcomes) == [1, 2, 3, 4]
        stored = V2Store(path).read()
        assert stored["revision"] == 4
        assert {account["username"] for account in stored["users"]} == {
            f"actor{i}" for i in range(4)
        }
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        queue.close()

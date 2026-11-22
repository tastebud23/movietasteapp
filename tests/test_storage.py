from types import SimpleNamespace
import pytest

from storage import AccountStore, GuestStore, read_all
from accounts import is_public_key


def test_guest_profiles_are_isolated_and_upserts_preserve_metadata():
    first, second = GuestStore({}), GuestStore({})
    first.table("profiles").upsert({"profile_name": "Alex", "taste_summary": "Mysteries"}).execute()
    first.table("profiles").upsert({"profile_name": "Alex"}).execute()
    assert second.table("profiles").select().execute().data == []
    assert first.table("profiles").select().execute().data == [{"profile_name": "Alex", "taste_summary": "Mysteries"}]


def test_guest_feedback_conflicts_and_deletion_are_scoped():
    store = GuestStore({})
    for profile in ("Alex", "Sam"):
        store.table("movie_feedback").upsert({"profile_name": profile, "movie_title": "Arrival", "status": "saved"}).execute()
    store.table("movie_feedback").upsert({"profile_name": "Alex", "movie_title": "Arrival", "status": "watched"}).execute()
    store.table("movie_feedback").delete().eq("profile_name", "Alex").execute()
    assert store.table("movie_feedback").select().execute().data == [{"profile_name": "Sam", "movie_title": "Arrival", "status": "saved"}]


class RecordingQuery:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return SimpleNamespace(data=[]) if name == "execute" else self
        return call


def test_account_reads_deletes_and_writes_include_owner():
    recorder = RecordingQuery()
    store = AccountStore(recorder, "actual-user")
    store.table("profiles").select().execute()
    assert ("table", ("movietaste_profiles",), {}) in recorder.calls
    assert ("eq", ("owner_id", "actual-user"), {}) in recorder.calls
    recorder.calls.clear()
    store.table("profiles").delete().eq("profile_name", "Alex").execute()
    assert ("eq", ("owner_id", "actual-user"), {}) in recorder.calls
    recorder.calls.clear()
    store.table("movie_feedback").upsert({"owner_id": "forged", "profile_name": "Alex", "movie_title": "Arrival"}, on_conflict="profile_name, movie_title").execute()
    mutation = next(c for c in recorder.calls if c[0] == "upsert")
    assert mutation[1][0]["owner_id"] == "actual-user"
    assert mutation[2]["on_conflict"] == "owner_id,profile_name,movie_title"


def test_auth_rejects_secret_and_service_role_keys():
    import base64
    import json
    def jwt(role):
        return "header." + base64.urlsafe_b64encode(json.dumps({"role": role}).encode()).decode().rstrip("=") + ".signature"
    assert is_public_key(jwt("anon"))
    assert is_public_key("sb_publishable_example")
    assert not is_public_key(jwt("service_role"))
    assert not is_public_key("sb_secret_example")


def test_guest_read_all_crosses_the_default_database_limit_without_lost_rows():
    rows = [{"profile_name": f"Profile {i:04d}"} for i in reversed(range(1205))]
    store = GuestStore({"profiles": rows})
    result = read_all(store, "profiles")
    assert [r["profile_name"] for r in result] == [f"Profile {i:04d}" for i in range(1205)]
    assert len({r["profile_name"] for r in result}) == 1205


def test_guest_range_is_inclusive_after_filtering_and_multiple_ordering():
    rows = [{"profile_name": name, "movie_title": title} for name, title in [
        ("Sam", "Arrival"), ("Jake", "Moon"), ("Jake", "Contact"), ("Jake", "Arrival")]]
    store = GuestStore({"movie_feedback": rows})
    page = (store.table("movie_feedback").select().eq("profile_name", "Jake")
            .order("profile_name").order("movie_title").range(1, 2).execute().data)
    assert [r["movie_title"] for r in page] == ["Contact", "Moon"]
    assert [r["movie_title"] for r in read_all(store, "movie_feedback", filters={"profile_name": "Jake"}, page_size=1)] == [
        "Arrival", "Contact", "Moon"]


def test_custom_order_keeps_stable_default_tie_breakers_across_pages():
    rows = [{"event_id": key, "created_at": date} for key, date in [
        ("d", "2026-11-22"), ("b", "2026-11-23"), ("c", "2026-11-23"), ("a", "2026-11-22")]]
    result = read_all(GuestStore({"recommendation_events": rows}), "recommendation_events",
                      orders=[("created_at", True)], page_size=2)
    assert [r["event_id"] for r in result] == ["b", "c", "a", "d"]


class CappedPostgrest:
    """A filtered server dataset with a row cap below the requested page size."""

    def __init__(self, rows, cap):
        self.rows, self.cap, self.requests = rows, cap, []

    def table(self, name):
        return CappedQuery(self, name)


class CappedQuery:
    def __init__(self, server, table):
        self.server = server
        self.request = {"table": table, "filters": {}, "orders": []}

    def select(self, columns="*"):
        return self

    def eq(self, field, value):
        self.request["filters"][field] = value
        return self

    def order(self, field, desc=False):
        assert not desc  # This server test exercises the ascending history path.
        self.request["orders"].append(field)
        return self

    def range(self, start, end):
        self.request["range"] = (start, end)
        return self

    def execute(self):
        self.server.requests.append(self.request)
        rows = [r for r in self.server.rows if all(r[k] == v for k, v in self.request["filters"].items())]
        rows.sort(key=lambda r: tuple(r[field] for field in self.request["orders"]))
        start, end = self.request["range"]
        return SimpleNamespace(data=rows[start:min(end + 1, start + self.server.cap)])


def test_account_paging_preserves_owner_filters_and_full_history_under_server_caps():
    desired = [{"owner_id": "Jake", "event_type": "feedback", "event_id": f"e{i:04d}",
                "created_at": f"2026-11-{1 + i % 23:02d}T00:00:00Z"} for i in range(1205)]
    other_owner = [{**r, "owner_id": "Someone else"} for r in desired]
    other_type = [{**r, "event_type": "solo"} for r in desired[:51]]
    server = CappedPostgrest(list(reversed(desired + other_owner + other_type)), cap=137)
    result = read_all(AccountStore(server, "Jake"), "recommendation_events", filters={"event_type": "feedback"})
    expected = sorted(desired, key=lambda r: (r["created_at"], r["event_id"]))
    assert result == expected
    assert len({r["event_id"] for r in result}) == 1205
    assert server.requests[-1]["range"][0] == 1205
    for request in server.requests:
        assert request["table"] == "movietaste_recommendation_events"
        assert request["filters"] == {"owner_id": "Jake", "event_type": "feedback"}
        assert request["orders"] == ["created_at", "event_id"]
        start, end = request["range"]
        assert end - start + 1 == 500


@pytest.mark.parametrize("page_size", [0, -1, True, 2.5])
def test_read_all_rejects_invalid_page_sizes(page_size):
    with pytest.raises(ValueError):
        read_all(GuestStore({}), "profiles", page_size=page_size)

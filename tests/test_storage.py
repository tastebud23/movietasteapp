from types import SimpleNamespace
from storage import AccountStore, GuestStore
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

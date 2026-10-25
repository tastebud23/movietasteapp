"""Session-only guest storage and account-scoped Supabase access.

The new tables intentionally do not reuse the legacy, globally named profiles.
Database row-level policies remain the authority for authenticated access.
"""
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4


TABLES = {name: f"movietaste_{name}" for name in (
    "profiles", "movie_feedback", "recommendation_events", "movie_cache"
)}
KEYS = {
    "profiles": "profile_name", "movie_feedback": "profile_name,movie_title",
    "recommendation_events": "event_id", "movie_cache": "watchmode_id",
}


class GuestStore:
    """One browser session's data; never shared through a global cache."""

    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        if name not in TABLES:
            raise ValueError("Unknown table")
        return GuestQuery(self.tables.setdefault(name, []), name)


class GuestQuery:
    def __init__(self, rows, name):
        self.rows, self.name = rows, name
        self.filters = []
        self.action, self.payload, self.sort = "select", None, None
        self.conflict = KEYS[name]

    def select(self, columns="*"):
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def order(self, key, desc=False):
        self.sort = (key, desc)
        return self

    def upsert(self, payload, on_conflict=None):
        self.action, self.payload = "upsert", payload
        self.conflict = on_conflict or self.conflict
        return self

    def insert(self, payload):
        self.action, self.payload = "insert", payload
        return self

    def delete(self):
        self.action = "delete"
        return self

    def execute(self):
        matches = [r for r in self.rows if all(r.get(k) == v for k, v in self.filters)]
        if self.action == "delete":
            self.rows[:] = [r for r in self.rows if r not in matches]
        elif self.action in {"insert", "upsert"}:
            matches = []
            payloads = self.payload if isinstance(self.payload, list) else [self.payload]
            keys = [k.strip() for k in self.conflict.split(",")]
            for payload in payloads:
                row = deepcopy(payload)
                if self.name == "recommendation_events":
                    row.setdefault("event_id", str(uuid4()))
                existing = next((r for r in self.rows if all(r.get(k) == row.get(k) for k in keys)), None)
                if self.action == "upsert" and existing is not None:
                    existing.update(row)
                    row = existing
                else:
                    self.rows.append(row)
                matches.append(row)
        if self.sort:
            key, desc = self.sort
            matches = sorted(matches, key=lambda r: r.get(key, ""), reverse=desc)
        return SimpleNamespace(data=deepcopy(matches))


class AccountStore:
    def __init__(self, client, owner_id):
        self.client, self.owner_id = client, owner_id

    def table(self, name):
        return AccountQuery(self.client.table(TABLES[name]), self.owner_id, KEYS[name])


class AccountQuery:
    def __init__(self, query, owner_id, conflict):
        self.query, self.owner_id, self.conflict = query, owner_id, conflict

    def select(self, columns="*"):
        self.query = self.query.select(columns).eq("owner_id", self.owner_id)
        return self

    def delete(self):
        self.query = self.query.delete().eq("owner_id", self.owner_id)
        return self

    def eq(self, key, value):
        self.query = self.query.eq(key, value)
        return self

    def order(self, key, desc=False):
        self.query = self.query.order(key, desc=desc)
        return self

    def _owned(self, payload):
        if isinstance(payload, list):
            return [{**p, "owner_id": self.owner_id} for p in payload]
        return {**payload, "owner_id": self.owner_id}

    def upsert(self, payload, on_conflict=None):
        conflict = "owner_id," + (on_conflict or self.conflict).replace(" ", "")
        self.query = self.query.upsert(self._owned(payload), on_conflict=conflict)
        return self

    def insert(self, payload):
        self.query = self.query.insert(self._owned(payload))
        return self

    def execute(self):
        return self.query.execute()

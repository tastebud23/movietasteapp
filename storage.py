"""Session-only guest storage and account-scoped Supabase access.

The new tables intentionally do not reuse the legacy, globally named profiles.
Database row-level policies remain the authority for authenticated access.
"""
from collections.abc import Mapping
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
READ_ORDERS = {
    "profiles": ("profile_name",),
    "movie_feedback": ("profile_name", "movie_title"),
    "recommendation_events": ("created_at", "event_id"),
    "movie_cache": ("watchmode_id",),
}


def read_all(store, table_name, *, filters=None, orders=None, page_size=500):
    """Read every matching row through inclusive, deterministically ordered pages.

    ``filters`` is a mapping of equality filters. ``orders`` can be one column
    name or a sequence of names / ``(column, descending)`` pairs. Missing table
    key columns are appended as tie-breakers. Each page goes through the store,
    retaining AccountStore's ownership filter.

    Advance by the actual result size and stop only at an empty page. This also
    handles a server row cap lower than the requested page size. This is an
    ordered read, not a transactional snapshot of concurrent edits.
    """
    if table_name not in READ_ORDERS:
        raise ValueError("Unknown table")
    if type(page_size) is not int or page_size < 1:
        raise ValueError("Page size must be a positive integer.")
    if filters is None:
        filters = {}
    if not isinstance(filters, Mapping) or any(not isinstance(k, str) or not k for k in filters):
        raise ValueError("Filters must map column names to equality values.")
    requested = [orders] if isinstance(orders, str) else orders
    ordering = []
    for item in requested or ():
        if isinstance(item, str):
            column, descending = item, False
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            column, descending = item
        else:
            raise ValueError("Order entries must be column names or (column, descending) pairs.")
        if not isinstance(column, str) or not column or type(descending) is not bool:
            raise ValueError("Order columns must be non-empty text and descending must be boolean.")
        if column not in [name for name, _ in ordering]:
            ordering.append((column, descending))
    for column in READ_ORDERS[table_name]:
        if column not in [name for name, _ in ordering]:
            ordering.append((column, False))

    rows, start = [], 0
    while True:
        query = store.table(table_name).select("*")
        for key, value in filters.items():
            query = query.eq(key, value)
        for column, descending in ordering:
            query = query.order(column, desc=descending)
        page = query.range(start, start + page_size - 1).execute().data
        if not isinstance(page, list):
            raise ValueError("Database read did not return a row list.")
        if not page:
            return rows
        rows.extend(page)
        start += len(page)


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
        self.action, self.payload = "select", None
        self.orders, self.window = [], None
        self.conflict = KEYS[name]

    def select(self, columns="*"):
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def order(self, key, desc=False):
        self.orders.append((key, desc))
        return self

    def range(self, start, end):
        if type(start) is not int or type(end) is not int or not 0 <= start <= end:
            raise ValueError("Range must contain non-negative inclusive bounds.")
        self.window = (start, end)
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
        for key, desc in reversed(self.orders):
            matches = sorted(matches, key=lambda r: (r.get(key) is None, r.get(key)), reverse=desc)
        if self.window is not None:
            start, end = self.window
            matches = matches[start:end + 1]
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

    def range(self, start, end):
        self.query = self.query.range(start, end)
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

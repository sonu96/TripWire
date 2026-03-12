"""Tests for tripwire/db/repositories/nonces.py."""

from tripwire.db.repositories.nonces import NonceRepository


class _MockResult:
    def __init__(self, data: list):
        self.data = data


class _MockQuery:
    def __init__(self, rows: list):
        self._rows = rows
        self._filters: dict[str, object] = {}

    def select(self, columns: str) -> "_MockQuery":
        return self

    def eq(self, column: str, value: object) -> "_MockQuery":
        self._filters[column] = value
        return self

    def execute(self) -> _MockResult:
        matched = []
        for row in self._rows:
            if all(row.get(k) == v for k, v in self._filters.items()):
                matched.append(row)
        return _MockResult(matched)


class _MockUpsertQuery:
    def __init__(self, rows: list, new_row: dict, ignore_duplicates: bool = False):
        self._rows = rows
        self._new_row = new_row
        self._ignore_duplicates = ignore_duplicates

    def execute(self) -> _MockResult:
        for existing in self._rows:
            if (
                existing.get("chain_id") == self._new_row.get("chain_id")
                and existing.get("nonce") == self._new_row.get("nonce")
                and existing.get("authorizer") == self._new_row.get("authorizer")
            ):
                # With ignore_duplicates=True, Supabase returns empty data
                if self._ignore_duplicates:
                    return _MockResult([])
                return _MockResult([existing])
        self._rows.append(self._new_row)
        return _MockResult([self._new_row])


class MockSupabaseTable:
    def __init__(self):
        self._rows: list[dict] = []

    def select(self, columns: str) -> _MockQuery:
        return _MockQuery(self._rows)

    def upsert(self, row: dict, **kwargs) -> _MockUpsertQuery:
        return _MockUpsertQuery(self._rows, row, ignore_duplicates=kwargs.get("ignore_duplicates", False))


class MockSupabase:
    def __init__(self):
        self._tables: dict[str, MockSupabaseTable] = {}

    def table(self, name: str) -> MockSupabaseTable:
        if name not in self._tables:
            self._tables[name] = MockSupabaseTable()
        return self._tables[name]


CHAIN_ID = 8453
NONCE = "0x" + "ab" * 32
AUTHORIZER = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def test_record_nonce_new():
    sb = MockSupabase()
    repo = NonceRepository(sb)
    result = repo.record_nonce(CHAIN_ID, NONCE, AUTHORIZER)
    assert result is True


def test_record_nonce_duplicate():
    sb = MockSupabase()
    repo = NonceRepository(sb)

    first = repo.record_nonce(CHAIN_ID, NONCE, AUTHORIZER)
    assert first is True

    second = repo.record_nonce(CHAIN_ID, NONCE, AUTHORIZER)
    assert second is False



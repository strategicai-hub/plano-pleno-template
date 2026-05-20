import pytest


class FakeRedis:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.deleted: list[str] = []

    async def get(self, key: str):
        return self.values.get(key)

    async def llen(self, key: str) -> int:
        return len(self.lists.get(key, []))

    async def exists(self, key: str) -> int:
        return 1 if key in self.values or key in self.lists else 0

    async def delete(self, *keys: str) -> int:
        count = 0
        for key in keys:
            if key in self.values:
                del self.values[key]
                count += 1
            if key in self.lists:
                del self.lists[key]
                count += 1
            self.deleted.append(key)
        return count


@pytest.mark.asyncio
async def test_clear_stale_legacy_block_deletes_empty_legacy_block(monkeypatch):
    from app.services import redis_keys as keys
    from app.services import redis_service as rds

    phone = "5511999990000"
    fake = FakeRedis()
    fake.values[keys.block_key(phone)] = "1"

    async def fake_get_redis():
        return fake

    monkeypatch.setattr(rds, "get_redis", fake_get_redis)

    assert await rds.clear_stale_legacy_block(phone) is True
    assert keys.block_key(phone) in fake.deleted


@pytest.mark.asyncio
async def test_clear_stale_legacy_block_keeps_named_human_block(monkeypatch):
    from app.services import redis_keys as keys
    from app.services import redis_service as rds

    phone = "5511999990000"
    fake = FakeRedis()
    fake.values[keys.block_key(phone)] = "human"

    async def fake_get_redis():
        return fake

    monkeypatch.setattr(rds, "get_redis", fake_get_redis)

    assert await rds.clear_stale_legacy_block(phone) is False
    assert keys.block_key(phone) not in fake.deleted

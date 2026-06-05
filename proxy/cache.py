import logging
from redis.asyncio import Redis


log = logging.getLogger("proxy.cache")
CACHE_TTL= 60 # seconds
REDIS_URL= "redis://redis:6379" #service name in the docker

def make_key(method: str, url: str, query_string: str) -> str:
    return f"cache:{method}:{url}:{query_string}"

async def get(redis: Redis, method:str, url: str, query_string: str) -> bytes | None:
    key = make_key(method, url, query_string)
    value = await redis.get(key)
    if value:
        log.debug(f"Cache HIT for {key}")
    return value

async def set(redis: Redis, method: str, url: str, query_string: str, body: bytes) -> None: 
    key = make_key(method, url, query_string)
    await redis.set(key, body, ex=CACHE_TTL)
    log.debug(f"Cache set for {key} with this TTL {CACHE_TTL}s")

async def clear_all(redis: Redis) -> int:
    keys = await redis.keys("cache:*")
    if keys:
        await redis.delete(*keys)
    log.info(f"Deleted all {len(keys)} keys")
    return len(keys)

async def clear_by_path(redis: Redis, path: str) -> int:
    pattern = f"cache:*:{path}:*"
    keys = await redis.keys(pattern)
    if not keys:
        return 0
    count = await redis.delete(*keys)
    log.info(f"Cache cleared for path={path} — {count} keys removed")
    return count

async def count_keys(redis: Redis) -> int:
    keys = await redis.keys("cache:*")
    return len(keys)
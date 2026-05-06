import asyncio
import itertools
import logging

from aiohttp import web

log = logging.getLogger("proxy.queue")

# Priority tiers (lower = served first)
PRIORITY_CRITICAL = 1
PRIORITY_STANDARD = 5
PRIORITY_BATCH    = 10

QUEUE_MAX_SIZE = 100
NUM_WORKERS    = 5

# Tiebreaker so same-priority requests are served FIFO
_counter = itertools.count()


def get_priority(request: web.Request) -> int:
    # X-Priority header overrides route-based priority
    raw = request.headers.get("X-Priority")
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            pass

    path = request.path
    if path.startswith("/premium") or path.startswith("/admin"):
        return PRIORITY_CRITICAL
    if path.startswith("/api"):
        return PRIORITY_STANDARD
    return PRIORITY_BATCH


async def worker(app: web.Application) -> None:
    queue: asyncio.PriorityQueue = app["queue"]

    while True:
        priority, _, (request, body, req_id, future) = await queue.get()
        log.debug(f"[{req_id}] Worker picked up (priority {priority}, queue size {queue.qsize()})")

        try:
            result = await app["forward"](app, request, body, req_id)
            future.set_result(result)
        except Exception as e:
            if not future.done():
                future.set_exception(e)
        finally:
            queue.task_done()


async def startup_queue(app: web.Application) -> None:
    app["queue"] = asyncio.PriorityQueue(maxsize=QUEUE_MAX_SIZE)
    app["workers"] = [
        asyncio.ensure_future(worker(app))
        for _ in range(NUM_WORKERS)
    ]
    log.info(f"Queue started with {NUM_WORKERS} workers (max size {QUEUE_MAX_SIZE})")


async def shutdown_queue(app: web.Application) -> None:
    for w in app["workers"]:
        w.cancel()
    await asyncio.gather(*app["workers"], return_exceptions=True)
    log.info("Queue workers stopped")


async def enqueue(app: web.Application, request: web.Request, body: bytes, req_id: str) -> web.Response:
    queue: asyncio.PriorityQueue = app["queue"]

    if queue.full():
        log.warning(f"[{req_id}] Queue full — rejecting request")
        return web.Response(status=429, text="Too many requests")

    priority = get_priority(request)
    future: asyncio.Future = asyncio.get_running_loop().create_future()

    await queue.put((priority, next(_counter), (request, body, req_id, future)))
    log.debug(f"[{req_id}] Queued with priority {priority} (queue size {queue.qsize()})")

    return await future

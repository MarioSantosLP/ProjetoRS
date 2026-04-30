
import logging
import asyncio
import time


log = logging.getLogger("queue_handler") #label

PRIORITY_NAMES= {
    "/premium": "critical",
    "/api": "standard",
}

default_priority = "low"

#lower value higher priority
PRIORITY_VALUES = {
    "critical": 1,
    "standard": 5,
    "low": 10,
}

max_queue_size = 100

queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=max_queue_size)

def get_priority(path, override=None)->tuple[int, str]: #override to check for headers
    if override:
        name = override.lower()
        if name in PRIORITY_VALUES:
            return PRIORITY_VALUES[name], name
    
    for prefix, name in PRIORITY_NAMES.items():
        if path.startswith(prefix):
            return PRIORITY_VALUES[name], name
        
    return PRIORITY_VALUES[default_priority], default_priority


class QueueFullError(Exception):
    pass


async def enqueue(req_id, path, override: str | None, handler) -> asyncio.Future:
    if queue.full():
        log.warning(f"[{req_id}] Queue full — rejecting request")
        raise QueueFullError()
    
    priority = get_priority(path, override)
    future = asyncio.get_event_loop().create_future()

    await queue.put((priority, time.monotonic(), req_id, handler, future))
    log.info(f"[{req_id}] enqueued priority={priority} depth={queue.qsize()}")

    return future

async def worker(worker_id: int) -> None:
    while True:
        priority, queued_at, req_id, handler, future = await queue.get()
        waited_ms = round((time.monotonic() - queued_at) * 1000)
        log.info(f"[{req_id}] worker-{worker_id} dequeued priority={priority} waited={waited_ms}ms")

        try:
            result = await handler()
            if not future.done():
                future.set_result(result)
        except Exception as e:
            if not future.done():
                future.set_exception(e)
        finally:
            queue.task_done()


async def start_workers(n: int = 5) -> None:
    for i in range(n):
        asyncio.create_task(worker(i), name=f"worker-{i}")
    log.info(f"Started {n} workers")



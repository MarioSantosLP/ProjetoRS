import logging
import uuid
import sys
import time
import os
import collections
import load_balancer as lb
from aiohttp import web, ClientSession, ClientTimeout
from logging.handlers import RotatingFileHandler
import asyncio
from priority_queue import enqueue, startup_queue, shutdown_queue
from circuit import CircuitBreaker

os.makedirs("logs", exist_ok=True) #so it doesnt fail if missing

#Logging (ts + msg) method used in lab7_8 (rotation idea from SO project)
handler = RotatingFileHandler(
    filename=f"logs/{sys.argv[0]}.log",
    maxBytes= 2 * 1024 * 1024 , #rotates when log file reaches 2MB
    backupCount=5,
)
handler.setFormatter(logging.Formatter(
    fmt="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
))

logging.basicConfig(level=logging.DEBUG, handlers=[handler])
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
log = logging.getLogger("proxy")

# the containers
CONTAINERS = [
    "http://web1:8000",
    "http://web2:8000",
]


#metrics created more for demonstration 
error_count = {container : 0 for container in CONTAINERS}
request_count = {container : 0 for container in CONTAINERS}
total_requests = 0
start_time = time.time()

#needed for status(should change when we do many load balancers later)
LOAD_BALANCER = "round_robin"

HEALTH_TTL = 3
health_cache = {
    container: {"reachable": False, "checked_at": 0}
    for container in CONTAINERS
}

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

circuit_breakers = {c: CircuitBreaker() for c in CONTAINERS}


# Keeps the last 500 request timelines in memory (older ones are evicted automatically)
TRACE_MAX = 500
_traces: collections.OrderedDict[str, list[dict]] = collections.OrderedDict()

def trace(req_id: str, component: str, event: str, **kwargs) -> None:
    """Append a timestamped event to the trace for req_id."""
    if req_id not in _traces:
        if len(_traces) >= TRACE_MAX:
            _traces.popitem(last=False)  # evict oldest
        _traces[req_id] = []
    entry = {"ts": round(time.monotonic(), 4), "component": component, "event": event, **kwargs}
    _traces[req_id].append(entry)
    log.debug(f"[{req_id}] {component} {event} {' '.join(f'{k}={v}' for k,v in kwargs.items())}")


async def metrics(request: web.Request) -> web.Response:
    return web.json_response({
        "uptime": round(time.time() - start_time, 2),
        "total_requests": total_requests,
        "requests_per_container": request_count,
        "errors_per_container": error_count,

    })

#helper to check if alive so we dont need to do it twice
async def ping_container(app: web.Application, container: str, force: bool = False) -> bool:
    now = time.time()
    cached = health_cache[container]

    if not force and now - cached["checked_at"] < HEALTH_TTL:
        return cached["reachable"]

    try:
        session = app["session"]
        async with session.get(f"{container}/ping", timeout=ClientTimeout(total=2)) as resp: #give it 2 secs before mark as down
            reachable = resp.status == 200
    except Exception:
        reachable = False

    health_cache[container] = {
        "reachable": reachable,
        "checked_at": now,
    }

    return reachable

async def startup_session(app: web.Application) -> None:
    app["session"] = ClientSession()

async def startup_forward(app: web.Application) -> None:
    app["forward"] = forward

async def close_session(app: web.Application) -> None:
    await app["session"].close()

async def startup_health_check(app: web.Application) -> None:
    log.info("Running startup health checks...")
    for container in CONTAINERS:
        reachable = await ping_container(app, container, force=True)
        log.info(f"{container} {'reachable' if reachable else 'unreachable'}")

async def status(request: web.Request) -> web.Response:
    containers = []
    for c in CONTAINERS:
        circuit = circuit_breakers[c].current_state  # read before ping so half_open is visible
        reachable = await ping_container(request.app, c, force=True)
        containers.append({
            "container": c,
            "reachable": reachable,
            "circuit": circuit,
        })
    return web.json_response({
        "load_balancer": LOAD_BALANCER,
        "containers": containers,
    })

async def handle(request: web.Request) -> web.Response:
    global total_requests
    req_id = str(uuid.uuid4())[:8]
    body = await request.read()  # must read here — stream can't be consumed inside the worker
    total_requests += 1
    log.info(f"[{req_id}] {request.method} {request.path} (from {request.remote})")
    trace(req_id, "gateway", "received", method=request.method, path=request.path, client=request.remote or "")
    return await enqueue(request.app, request, body, req_id)

async def forward(app: web.Application, request: web.Request, body: bytes, req_id: str) -> web.Response:
    workload_type = request.headers.get("X-Workload-Type", "").lower() or None
    if workload_type not in ("cpu", "memory"):
        workload_type = None

    t_start = time.monotonic()
    trace(req_id, "queue", "dequeued", workload=workload_type or "any")

    container = await lb.pick_by_role(workload_type, LOAD_BALANCER, app["session"])

    if container is None:
        log.error(f"[{req_id}] No available containers")
        trace(req_id, "balancer", "no_container")
        return web.Response(status=503, text="No available containers")

    if circuit_breakers[container].is_open():
        log.warning(f"[{req_id}] Circuit open for {container}")
        trace(req_id, "circuit", "open", container=container)
        return web.Response(status=503, text="No available containers")

    if not await ping_container(app, container):
        circuit_breakers[container].record_failure()
        trace(req_id, "health", "unreachable", container=container)
        return web.Response(status=503, text="No available containers")

    request_count[container] += 1
    trace(req_id, "balancer", "routed", container=container, algorithm=LOAD_BALANCER,
          cpu=round(lb.container_stats[container]["cpu"], 1),
          mem=round(lb.container_stats[container]["mem"], 1))

    log.info(f"[{req_id}] {request.method} {request.path} → {container} (workload={workload_type or 'any'})")

    url = f"{container}{request.rel_url}"  # rel_url preserves query params (?id=10)

    incoming_headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }

    existing_xff = request.headers.get("X-Forwarded-For")
    client_ip = request.remote or ""

    if existing_xff and client_ip:
        x_forwarded_for = f"{existing_xff}, {client_ip}"
    else:
        x_forwarded_for = existing_xff or client_ip

    incoming_headers["X-Request-ID"] = req_id
    incoming_headers["X-Forwarded-For"] = x_forwarded_for

    try:
        session = app["session"]
        t_sent = time.monotonic()
        async with session.request(
            method=request.method,
            url=url,
            headers=incoming_headers,
            data=body,
            timeout=ClientTimeout(total=10),
        ) as resp:
            resp_body = await resp.read()
            elapsed_ms = round((time.monotonic() - t_start) * 1000)
            backend_ms = round((time.monotonic() - t_sent) * 1000)
            log.info(f"[{req_id}] ← {resp.status} from {container}")
            circuit_breakers[container].record_success()
            trace(req_id, "proxy", "responded", status=resp.status,
                  total_ms=elapsed_ms, backend_ms=backend_ms, container=container)
            response_headers = {
                key: value
                for key, value in resp.headers.items()
                if key.lower() not in HOP_BY_HOP_HEADERS
            }
            return web.Response(
                status=resp.status,
                body=resp_body,
                headers=response_headers,
            )

    except Exception as e:
        error_count[container] += 1
        log.error(f"[{req_id}] Failed to reach {container}: {e}")
        circuit_breakers[container].record_failure()
        trace(req_id, "proxy", "error", container=container, error=str(e))
        return web.Response(status=502, text="Container unavailable")

async def trace_endpoint(request: web.Request) -> web.Response:
    req_id = request.match_info["req_id"]
    events = _traces.get(req_id)
    if events is None:
        return web.json_response({"error": f"No trace found for '{req_id}'"}, status=404)

    # Compute relative ms from first event so timeline is easy to read
    t0 = events[0]["ts"] if events else 0
    timeline = [{**e, "ms": round((e["ts"] - t0) * 1000)} for e in events]

    return web.json_response({"req_id": req_id, "events": timeline})


async def startup_lb_loops(app: web.Application) -> None:
    asyncio.ensure_future(lb.health_loop())
    asyncio.ensure_future(lb.active_probe_loop(app["session"]))
    log.info("Load balancer loops started")

app = web.Application()
app.on_startup.append(startup_session)
app.on_startup.append(startup_forward)
app.on_startup.append(startup_queue)
app.on_startup.append(startup_health_check) #basically for debug 
app.on_startup.append(startup_lb_loops) # start load balancer background loops on startup
app.on_cleanup.append(shutdown_queue)
app.on_cleanup.append(close_session)
app.router.add_get("/metrics", metrics)
app.router.add_get("/status", status)
app.router.add_get("/trace/{req_id}", trace_endpoint)
app.router.add_route("*", "/{path_info:.*}", handle)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8080)
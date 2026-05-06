import logging
import uuid
import sys
import time
import os

from aiohttp import web, ClientSession, ClientTimeout
from logging.handlers import RotatingFileHandler
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

# Round robin could have used itertools.cycle
_rr_index = 0

circuit_breakers = {c: CircuitBreaker() for c in CONTAINERS}

async def next_container(app: web.Application) -> str | None:
    global _rr_index

    for _ in range(len(CONTAINERS)):
        container = CONTAINERS[_rr_index % len(CONTAINERS)]
        _rr_index += 1

        if circuit_breakers[container].is_open():
            log.warning(f"Skipping {container} — circuit open")
            continue

        if await ping_container(app, container): #keep in mind i might change this when we add the other load balancers
            return container

        circuit_breakers[container].record_failure()
        log.warning(f"Skipping unreachable container: {container}")

    return None

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
    return await enqueue(request.app, request, body, req_id)

async def forward(app: web.Application, request: web.Request, body: bytes, req_id: str) -> web.Response:
    container = await next_container(app)

    if container is None:
        log.error(f"[{req_id}] No available containers")
        return web.Response(status=503, text="No available containers")

    request_count[container] += 1

    log.info(f"[{req_id}] {request.method} {request.path} → {container}")

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
        async with session.request(
            method=request.method,
            url=url,
            headers=incoming_headers,
            data=body,
            timeout=ClientTimeout(total=10),
        ) as resp:
            resp_body = await resp.read()
            log.info(f"[{req_id}] ← {resp.status} from {container}")
            circuit_breakers[container].record_success()
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
        return web.Response(status=502, text="Container unavailable")


app = web.Application()
app.on_startup.append(startup_session)
app.on_startup.append(startup_forward)
app.on_startup.append(startup_queue)
app.on_startup.append(startup_health_check)  # basically for debug
app.on_cleanup.append(shutdown_queue)
app.on_cleanup.append(close_session)
app.router.add_get("/metrics", metrics)
app.router.add_get("/status", status)
app.router.add_route("*", "/{path_info:.*}", handle)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8080)
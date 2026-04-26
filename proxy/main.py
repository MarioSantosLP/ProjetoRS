import asyncio

import logging
import uuid
import sys
import time

from aiohttp import web, ClientSession, ClientTimeout
from logging.handlers import RotatingFileHandler

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
request_count ={container : 0 for container in CONTAINERS}
total_requests = 0
start_time = time.time()

#needed for status(should change when we do many load balancers later)
LOAD_BALANCER = "round_robin"

# Round robin could have used itertools.cycle
_rr_index = 0

async def next_container() -> str | None:
    global _rr_index

    for _ in range(len(CONTAINERS)):
        container = CONTAINERS[_rr_index % len(CONTAINERS)]
        _rr_index += 1

        if await ping_container(container):
            return container

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
async def ping_container(container: str) -> bool:
    try:
        async with ClientSession() as session:
            async with session.get(f"{container}/ping", timeout=ClientTimeout(total=2)) as resp: #give it 2 secs before mark as down
                return resp.status == 200
    except Exception:
        return False

async def startup_health_check(app: web.Application) -> None:
    log.info("Running startup health checks...")
    for container in CONTAINERS:
        reachable = await ping_container(container)
        log.info(f"{container} {'reachable' if reachable else 'unreachable'}")

async def status(request: web.Request) -> web.Response:
    containers = [
        {"container": c, "reachable": await ping_container(c)}
        for c in CONTAINERS
    ]
    return web.json_response({
        "load_balancer": LOAD_BALANCER,
        "containers": containers,
    })

async def handle(request: web.Request) -> web.Response:

    global total_requests
    # give ID to this request
    req_id = str(uuid.uuid4())[:8]

    # load balance the containers
    container = await next_container() #also make it wait for ping

    if container is None:
        log.error(f"[{req_id}] No available containers")
        return web.Response(status=503, text="No available containers")
    
    request_count[container] += 1
    total_requests += 1

    log.info(f"[{req_id}] {request.method} {request.path} → {container} (from {request.remote})")

    
    url = f"{container}{request.rel_url}" #better to use rel_url to keep query params  (?id=10 ) for example

    # Forward the request, passing id
    try:
        async with ClientSession() as session:
            async with session.request(
                method=request.method,
                url=url,
                headers={
                    "X-Request-ID": req_id,
                    "X-Forwarded-For": request.remote or "",
                },
                data=await request.read(),
                timeout=ClientTimeout(total=10),
            ) as resp:
                body = await resp.read()
                log.info(f"[{req_id}] ← {resp.status} from {container}")
                return web.Response(
                    status=resp.status,
                    body=body,
                    content_type="application/json",
                )

    except Exception as e:
        error_count[container] += 1
        log.error(f"[{req_id}] Failed to reach {container}: {e}")
        return web.Response(status=502, text="Container unavailable")




app = web.Application()
app.on_startup.append(startup_health_check) #basically for debug 
app.router.add_get("/metrics", metrics)
app.router.add_get("/status", status)
app.router.add_route("*", "/{path_info:.*}", handle)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8080)
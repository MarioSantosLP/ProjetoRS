import asyncio
import collections
import logging
import os
import sys
import time
import uuid
from json import JSONDecodeError
from logging.handlers import RotatingFileHandler

import cache
from redis.asyncio import Redis

import aiohttp
import grpc
from aiohttp import ClientSession, ClientTimeout, web

import load_balancer as lb
import service_pb2
import service_pb2_grpc
from circuit import CircuitBreaker
from priority_queue import enqueue, shutdown_queue, startup_queue


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


HEALTH_TTL = 3

#metrics 
error_count:   dict[str, int] = {}
request_count: dict[str, int] = {}
total_requests = 0
start_time = time.time()



LOAD_BALANCER = "cpu_aware" #weighted, cpu_aware, active_probe, round_robin 

health_cache:    dict[str, dict] = {}
circuit_breakers: dict[str, CircuitBreaker] = {}
 
HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}



# Keeps the last 500 request timelines in memory (older ones are evicted automatically)
TRACE_MAX = 500
_traces: collections.OrderedDict[str, list[dict]] = collections.OrderedDict()



#helper funcs
def _init_container(container: str) -> None:
    
    error_count.setdefault(container, 0)
    request_count.setdefault(container, 0)
    health_cache.setdefault(container, {"reachable": False, "checked_at": 0})
    circuit_breakers.setdefault(container, CircuitBreaker())
 
 
def _remove_container(container: str) -> None: #changed from init
    health_cache.pop(container, None)
    circuit_breakers.pop(container, None)
    error_count.pop(container, None)
    request_count.pop(container, None)


def trace(req_id: str, component: str, event: str, **kwargs) -> None:
    if req_id not in _traces:
        if len(_traces) >= TRACE_MAX:
            _traces.popitem(last=False)  # evict oldest
        _traces[req_id] = []
    entry = {"ts": round(time.monotonic(), 4), "component": component, "event": event, **kwargs}
    _traces[req_id].append(entry)
    if log.isEnabledFor(logging.DEBUG):
        details = " ".join(f"{k}={v}" for k, v in kwargs.items())
        if details:
            log.debug("[%s] %s %s %s", req_id, component, event, details)
        else:
            log.debug("[%s] %s %s", req_id, component, event)


async def metrics(request: web.Request) -> web.Response:
    return web.json_response({
        "uptime": round(time.time() - start_time, 2),
        "total_requests": total_requests,
        "requests_per_container": request_count,
        "errors_per_container": error_count,
        "container_stats": lb.container_stats,
        "probe_stats": lb.probe_stats,
        "cache": {
            "keys_in_cache": await cache.count_keys(request.app["redis"]),
        },
    })

#basically a func to keep build a cache of pings instead of doing one every time
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

async def startup_forward(app: web.Application) -> None: #just so there is no need to import 
    app["forward"] = forward

async def close_session(app: web.Application) -> None:
    await app["session"].close()

async def startup_config(app: web.Application) -> None: 
    try:
        lb.load_config()
        for container in lb.CONTAINERS:
            _init_container(container)
        log.info(f"Containers loaded: {lb.CONTAINERS}")
    except FileNotFoundError:
        log.error("config.json not found")
    except JSONDecodeError:
        log.error("config.json is not valid JSON")

async def startup_health_check(app: web.Application) -> None:
    log.info("Running startup health checks...")
    for container in lb.CONTAINERS:
        reachable = await ping_container(app, container, force=True)
        log.info(f"{container} {'reachable' if reachable else 'unreachable'}")

async def startup_lb_loops(app: web.Application) -> None:
    #startup the lb loops in background
    
    asyncio.ensure_future(lb.health_loop())
    asyncio.ensure_future(lb.active_probe_loop(app["session"]))
    log.info("Load balancer loops started")


async def startup_grpc_channels(app: web.Application) -> None:
    app["grpc_channels"] = {
        container: grpc.aio.insecure_channel(lb.GRPC_URLS[container])
        for container in lb.CONTAINERS
    }
    log.info("gRPC channels created")

async def close_grpc_channels(app: web.Application) -> None:
    for channel in app["grpc_channels"].values():
        await channel.close()
    log.info("gRPC channels closed")


async def startup_redis(app: web.Application) -> None:
    app["redis"] = Redis.from_url(cache.REDIS_URL, decode_responses=False)
    log.info("Redis connection established")

async def close_redis(app: web.Application) -> None:
    await app["redis"].aclose()
    log.info("Redis connection closed")

async def status(request: web.Request) -> web.Response:
    containers = []
    for c in lb.CONTAINERS:
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


async def admin_reload(request: web.Request) -> web.Response:
  
    try:
        added, removed = lb.reload_config()
 
        for container in added:
            _init_container(container)
            request.app["grpc_channels"][container] = grpc.aio.insecure_channel(lb.GRPC_URLS[container])
            reachable = await ping_container(request.app, container, force=True)
            log.info(f"[reload] New container {container} — reachable: {reachable}")
 
        for container in removed:
            _remove_container(container)
            ch = request.app["grpc_channels"].pop(container, None)
            if ch:
                await ch.close()
            log.info(f"[reload] Removed container {container}")
 
        return web.json_response({
            "status":   "ok",
            "added":    added,
            "removed":  removed,
            "active":   lb.CONTAINERS,
        })
 
    except FileNotFoundError:
        log.error("[reload] config.json not found")
        return web.json_response({"error": "config.json not found"}, status=500)
    except Exception as e:
        log.error(f"[reload] Failed: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def handle(request: web.Request) -> web.StreamResponse:
    if (request.headers.get("Upgrade","").lower() == "websocket"):
        #they dont go into priority queue because its a long lived conn and it would block the queue
        return await ws_handle(request)
    global total_requests
    req_id = str(uuid.uuid4())[:8]
    body = await request.read()  # must read here stream can't be consumed inside the worker (bug fixed: no more empty body)
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

    wants_cache = request.headers.get("X-Cache", " ").lower() == "true"
    if wants_cache:
        cached = await cache.get(app["redis"], request.method, request.path, request.query_string)
        if cached:
            trace(req_id, "cache", "hit", path=request.path)
            log.info(f"[{req_id}] Cache HIT for {request.method} {request.path}")
            return web.Response(status=200, body=cached, content_type="application/json")

    container = None
    tried: set[str] = set()

    for _ in range(len(lb.CONTAINERS)):
        candidate = await lb.pick_by_role(
            workload_type,
            LOAD_BALANCER,
            app["session"],
            exclude=tried,
        )

        if candidate is None:
            break

        tried.add(candidate)

        if circuit_breakers[candidate].is_open():
            log.warning(f"[{req_id}] Circuit open for {candidate}, trying another")
            trace(req_id, "circuit", "open", container=candidate)
            continue

        if not lb.probe_stats.get(candidate, {}).get("healthy", False):
            circuit_breakers[candidate].record_failure()
            trace(req_id, "health", "unreachable", container=candidate)
            continue

        container = candidate
        break

    if container is None:
        log.error(f"[{req_id}] No available containers")
        trace(req_id, "balancer", "no_container")
        return web.Response(status=503, text="No available containers")

    request_count[container] += 1
    lb.conn_acquired(container)  # increment active connections for weighted lb
    trace(req_id, "balancer", "routed", container=container, algorithm=LOAD_BALANCER,
          cpu=round(lb.container_stats[container]["cpu"], 1),
          mem=round(lb.container_stats[container]["mem"], 1))

    log.info(f"[{req_id}] {request.method} {request.path} → {container} (workload={workload_type or 'any'})")

    # filtra headers hop-by-hop
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP_HEADERS
    }

    # always inject tracing and forwarding headers
    existing_xff = request.headers.get("X-Forwarded-For", "")
    client_ip = request.remote or ""
    headers["X-Forwarded-For"] = f"{existing_xff}, {client_ip}".strip(", ") if existing_xff else client_ip
    headers["X-Request-ID"] = req_id

    try:
        t_sent = time.monotonic()
        channel = app["grpc_channels"].get(container)
        if channel is None:
            raise RuntimeError(f"No gRPC channel for {container}")
        stub = service_pb2_grpc.WebServiceStub(channel)
        grpc_request = service_pb2.HttpRequest(
            method=request.method,
            path=request.path,
            body=body,
            headers=headers,
        )
        grpc_response = await stub.HandleRequest(grpc_request)
        elapsed_ms = round((time.monotonic() - t_start) * 1000)
        backend_ms = round((time.monotonic() - t_sent) * 1000)
        circuit_breakers[container].record_success()
        log.info(f"[{req_id}] ← {grpc_response.status} from {container}")
        trace(req_id, "proxy", "responded", status=grpc_response.status,
              total_ms=elapsed_ms, backend_ms=backend_ms, container=container)
        if wants_cache:
            await cache.set(app["redis"], request.method, request.path, request.query_string, grpc_response.body)
            trace(req_id, "cache", "stored", path=request.path, ttl=cache.CACHE_TTL)
        return web.Response(
            status=grpc_response.status,
            body=grpc_response.body,
            headers=dict(grpc_response.headers),
        )

    except Exception as e:
        error_count[container] += 1
        circuit_breakers[container].record_failure()
        trace(req_id, "proxy", "error", container=container, error=str(e))
        log.error(f"[{req_id}] gRPC failed for {container}: {e}")
        return web.Response(status=502, text="Container unavailable")

    finally:
        lb.conn_released(container)

async def trace_endpoint(request: web.Request) -> web.Response:
    req_id = request.match_info["req_id"]
    events = _traces.get(req_id)
    if events is None:
        return web.json_response({"error": f"No trace found for '{req_id}'"}, status=404)

    # Compute relative ms from first event so timeline is easy to read
    t0 = events[0]["ts"] if events else 0
    timeline = [{**e, "ms": round((e["ts"] - t0) * 1000)} for e in events]

    return web.json_response({"req_id": req_id, "events": timeline})

async def admin_cache_clear(request: web.Request) -> web.Response:
    path = request.rel_url.query.get("path")
    redis = request.app["redis"]
    if path:
        count = await cache.clear_by_path(redis, path)
    else:
        count = await cache.clear_all(redis)
    return web.json_response({"status": "ok", "keys_removed": count})

async def ws_handle(request: web.Request) -> web.StreamResponse:
    req_id = str(uuid.uuid4())[:8]
    t_start = time.monotonic()

    trace(req_id, "websocket", "received", path=request.path, client=request.remote or "")
    
    container = None
    tried: set[str] = set()

    for _ in range(len(lb.CONTAINERS)):
        candidate = await lb.pick_by_role(
            None,
            LOAD_BALANCER,
            request.app["session"],
            exclude=tried,
        )

        if candidate is None:
            break

        tried.add(candidate)

        if circuit_breakers[candidate].is_open():
            trace(req_id, "circuit", "open", container=candidate)
            continue

        if not lb.probe_stats.get(candidate, {}).get("healthy", False):
            circuit_breakers[candidate].record_failure()
            trace(req_id, "health", "unreachable", container=candidate)
            continue

        container = candidate
        break

    if container is None:
        trace(req_id, "websocket", "no_container")
        return web.Response(status=503, text="No containers available")
    
    ws_client = web.WebSocketResponse()
    await ws_client.prepare(request)

    backend_url = container.replace("http://", "ws://") + "/ws"
    session = request.app["session"]

    n_sent=0
    n_recv=0
    
    try:
        async with session.ws_connect(backend_url) as ws_backend:

            circuit_breakers[container].record_success()
            trace(req_id, "websocket", "connected", container=container)
            
            async def client_to_backend():
                nonlocal n_sent
                async for msg in ws_client:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        n_sent += 1
                        await ws_backend.send_str(msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        n_sent += 1
                        await ws_backend.send_bytes(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                        break

            async def backend_to_client():
                nonlocal n_recv
                async for msg in ws_backend:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        n_recv += 1
                        await ws_client.send_str(msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        n_recv += 1
                        await ws_client.send_bytes(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                        break

            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(client_to_backend()),
                    asyncio.create_task(backend_to_client()),
                ],
                return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()

    except Exception as e:
        circuit_breakers[container].record_failure()
        trace(req_id, "websocket", "error", container=container, error=str(e))
        log.error(f"[{req_id}] WebSocket failed for {container}: {e}")
        await ws_client.close()
        return ws_client
    
    trace(req_id, "websocket", "disconnected", container=container, 
          duration_s=round(time.monotonic()-t_start, 2), messages_sent=n_sent, messages_receives=n_recv)
        
    await ws_client.close()
    return ws_client


app = web.Application()
app.on_startup.append(startup_session)
app.on_startup.append(startup_forward)
app.on_startup.append(startup_config)
app.on_startup.append(startup_queue)
app.on_startup.append(startup_health_check) #basically for debug 
app.on_startup.append(startup_lb_loops) # start load balancer background loops on startup
app.on_startup.append(startup_grpc_channels)
app.on_startup.append(startup_redis)
app.on_cleanup.append(shutdown_queue)
app.on_cleanup.append(close_grpc_channels)
app.on_cleanup.append(close_redis)
app.on_cleanup.append(close_session)

app.router.add_get("/metrics", metrics)
app.router.add_get("/status", status)
app.router.add_get("/trace/{req_id}", trace_endpoint)
app.router.add_post("/admin/reload", admin_reload)
app.router.add_post("/admin/cache/clear", admin_cache_clear)
app.router.add_route("*", "/{path_info:.*}", handle)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8080)
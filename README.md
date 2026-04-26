# Reverse Proxy & Load Balancer — Project Notes

> Tema 1 – Universidade de Aveiro  
> Internal doc — not a deliverable

---

## What we're building

An async reverse proxy in Python that:
- Receives HTTP/WebSocket/gRPC requests and forwards them to Docker containers
- Inspects headers (X-Forwarded-For, X-Priority) to decide routing
- Uses an `asyncio.PriorityQueue` to process requests by priority
- Routes intelligently based on real container CPU usage (via docker stats)
- Traces every request end-to-end so we can see exactly what the system is doing
- Benchmarks multiple load balancing approaches and compares them

---

## Architecture

```
Clients (HTTP / WebSocket / gRPC)
           │
           ▼
   ┌───────────────┐
   │   Gateway     │  ← detects protocol, inspects headers, assigns request ID
   └───────┬───────┘
           │
           ▼
   ┌───────────────┐
   │ Priority Queue│  ← asyncio.PriorityQueue, N workers, circuit breaker
   │  critical=1   │
   │  standard=5   │
   │  batch=10     │
   └───────┬───────┘
           │
           ▼
   ┌───────────────┐
   │ Load Balancer │  ← reads live CPU from docker stats, falls back to round robin
   └──┬──────┬─────┘
      │      │
   [web1]  [web2]  [api1]  [api2]  [worker1..3]
```

---

## Stack

| Thing | Tool |
|---|---|
| Proxy core | Python `asyncio` + `aiohttp` |
| Container stats | `aiodocker` (reads CPU/mem from Docker API) |
| Internal protocol | `grpcio` for gRPC backends |
| Load testing | `locust` |
| Unit tests | `pytest-asyncio` |
| Config | `PyYAML` |
| Containers | Docker Compose |

**Why not Nginx/HAProxy?** They can't do a custom priority queue without Lua scripting nightmares. Python asyncio maps 1:1 to what the brief asks for. We could put Nginx in front just for TLS — worth mentioning in the presentation as that's how real stacks work.

---

## Project Structure

```
project/
├── proxy/
│   ├── main.py             # starts server, workers, health loop, tracer
│   ├── gateway.py          # protocol detection, header inspection, request ID injection
│   ├── queue_handler.py    # asyncio.PriorityQueue + workers
│   ├── balancer.py         # CPU-aware routing + round robin fallback
│   ├── circuit_breaker.py  # per-backend failure tracking
│   ├── tracer.py           # end-to-end request tracing
│   ├── metrics.py          # /metrics endpoint
│   ├── admin.py            # /admin/* endpoints + hot reload
│   ├── config.py           # loads config.yaml
│   └── Dockerfile
│
├── services/
│   ├── web/                # FastAPI HTTP backend (spin up 2 instances)
│   ├── api/                # gRPC backend (spin up 2 instances)
│   └── worker/             # async task backend (spin up 3 instances)
│
├── benchmark/
│   ├── locustfile.py       # load test scenarios
│   └── results/            # store benchmark outputs here (json/csv)
│
├── tests/
│   ├── test_queue.py
│   ├── test_balancer.py
│   └── test_circuit_breaker.py
│
├── config.yaml
└── docker-compose.yml
```

---

## Components

### Gateway

Entry point. Every request goes through here first.

- Detects protocol (HTTP, WebSocket, gRPC) and routes to the right handler
- Reads `X-Forwarded-For` to potentially pin a request to a specific backend
- Reads `X-Priority` header if set, otherwise assigns priority by route
- **Injects a unique `X-Request-ID`** into every request (this feeds the tracer)

```python
async def handle(request: web.Request) -> web.Response:
    request_id = str(uuid.uuid4())
    request = request.clone(headers={**request.headers, "X-Request-ID": request_id})

    if request.headers.get("Upgrade") == "websocket":
        return await handle_websocket(request)
    if request.content_type == "application/grpc":
        return await handle_grpc(request)
    return await handle_http(request)
```

---

### Priority Queue

Every request enters the queue after the gateway. Workers pull from it.

SLA tiers (set in `config.yaml`, also overridable via `X-Priority` header):

| Tier | Value | Routes |
|---|---|---|
| Critical | 1 | `/premium`, `/admin` |
| Standard | 5 | `/api/*` |
| Batch | 10 | `/export`, `/report`, everything else |

```python
queue = asyncio.PriorityQueue(maxsize=500)

async def enqueue(request):
    if queue.full():
        raise web.HTTPTooManyRequests()  # 429, never let it grow unbounded
    priority = get_priority(request)
    await queue.put((priority, time.monotonic(), request))  # monotonic breaks ties

async def worker(worker_id):
    while True:
        priority, queued_at, request = await queue.get()
        await forward_request(request)
        queue.task_done()
```

Also implement:
- **Circuit breaker** — 3 failures in 10s → mark backend unhealthy, retry after cooldown
- **Request dedup** — same method+path+body hash within 1s → one upstream call, fan out response

---

### Load Balancer — the interesting part

We implement **4 approaches**, run them under the same load, and compare results. The point is to show we tried different things and understand the tradeoffs. The active algorithm is switchable via `config.yaml`.

#### Approach A — Round Robin (baseline)
Blind rotation, ignores server state entirely. First thing we implement, also used as the fallback.

```python
_cycle = itertools.cycle(backends)

def pick_round_robin():
    return next(_cycle)
```

#### Approach B — CPU-aware via docker stats
Background loop polls every 5s using `aiodocker`. Routes to least CPU loaded backend.

```python
import aiodocker

async def health_loop():
    async with aiodocker.Docker() as docker:
        while True:
            for backend in backends:
                container = await docker.containers.get(backend.container_name)
                stats = await container.stats(stream=False)

                cpu_delta = (stats["cpu_stats"]["cpu_usage"]["total_usage"]
                           - stats["precpu_stats"]["cpu_usage"]["total_usage"])
                sys_delta = (stats["cpu_stats"]["system_cpu_usage"]
                           - stats["precpu_stats"]["system_cpu_usage"])
                backend.cpu = (cpu_delta / sys_delta) * 100
                backend.healthy = backend.cpu < 90
                backend.last_seen = time.monotonic()
            await asyncio.sleep(5)

def pick_cpu_aware():
    fresh = [b for b in backends
             if b.healthy and time.monotonic() - b.last_seen < 15]
    if fresh:
        return min(fresh, key=lambda b: b.cpu)
    return pick_round_robin()  # stale data → fall back
```

> The proxy container needs access to the Docker socket:
> ```yaml
> proxy:
>   volumes:
>     - /var/run/docker.sock:/var/run/docker.sock
> ```
> This is how tools like Portainer and Traefik work — worth a mention in the presentation.

#### Approach C — Active probing (least response time)
Before routing, send a cheap `/ping` to each backend and pick the fastest responder.

```python
async def probe(backend) -> float:
    start = time.monotonic()
    try:
        await session.get(f"http://{backend.host}/ping", timeout=ClientTimeout(total=0.5))
        return time.monotonic() - start
    except:
        return float("inf")  # unreachable = skip it

async def pick_least_response_time():
    times = await asyncio.gather(*[probe(b) for b in backends])
    return backends[times.index(min(times))]
```

Note: adds probe latency to every request. The benchmark will show this is better for bursty traffic, worse under sustained load.

#### Approach D — Weighted score (CPU + memory + connections)
Combines multiple signals from docker stats into one score.

```python
def load_score(backend) -> float:
    return (
        backend.cpu * 0.5 +
        backend.mem_percent * 0.3 +
        (backend.active_connections / backend.capacity) * 0.2
    )

def pick_weighted():
    fresh = [b for b in backends if b.healthy]
    return min(fresh, key=load_score) if fresh else pick_round_robin()
```

#### Benchmark plan
Run each approach with locust: 200 users, 60s, same backend setup. Collect:
- P50 / P95 / P99 latency
- Throughput (req/s)
- Error rate

Save raw results to `benchmark/results/<approach>.json`. We present a comparison table in the report. Expected conclusion: "CPU-aware wins under sustained load, active probing wins for bursty traffic, weighted is most fair but adds complexity."

---

### Request Tracing

Every request gets a unique ID at the gateway. Every component logs that ID + timestamp. We can reconstruct the full lifecycle of any request.

```python
# tracer.py
import logging, time

logger = logging.getLogger("tracer")

def log(request_id: str, component: str, event: str, **kwargs):
    logger.info(
        f"[{request_id}] {component:12} {event:12} " +
        " ".join(f"{k}={v}" for k, v in kwargs.items())
    )
```

Each component calls it:

```python
# gateway.py
tracer.log(req_id, "gateway", "received", method=request.method, path=request.path)

# queue_handler.py
tracer.log(req_id, "queue", "enqueued", priority=priority, depth=queue.qsize())
tracer.log(req_id, "worker-3", "dequeued", waited_ms=round((time.monotonic() - queued_at) * 1000))

# balancer.py
tracer.log(req_id, "balancer", "routed", backend=target.name, cpu=target.cpu)

# forward_request
tracer.log(req_id, "proxy", "responded", status=resp.status, total_ms=elapsed)
```

Output looks like:

```
[abc-123] gateway      received     method=GET path=/api/items
[abc-123] queue        enqueued     priority=5 depth=12
[abc-123] worker-3     dequeued     waited_ms=44
[abc-123] balancer     routed       backend=web2 cpu=23.1
[abc-123] proxy        responded    status=200 total_ms=91
```

Also add `GET /trace/<request_id>` that returns the full timeline for a given ID — useful for the demo.

**Why this matters for the demo:** Show two requests side by side — one priority 1, one priority 10. The trace literally shows when each was picked up from the queue. Priority system becomes impossible to argue with.

---

### Observability

`GET /metrics` — expose these:
- `queue_depth` per tier (critical / standard / batch)
- `requests_total` labelled by backend and status
- `backend_cpu` per backend (from docker stats)
- `circuit_breaker_state` per backend (0=ok, 1=open)
- `p95_latency` per route

`GET /dashboard` — single HTML file, polls `/metrics` every second. Shows backend CPU bars, queue depth per tier, circuit breaker status lights (green/red). No external dependencies, just vanilla JS.

`POST /admin/reload` — re-reads `config.yaml` without restart. Add a backend, hit reload, it immediately gets traffic.

---

## Config

```yaml
proxy:
  host: "0.0.0.0"
  port: 80
  workers: 10
  max_queue_depth: 500
  health_poll_interval: 5      # seconds between docker stats polls
  stale_threshold: 15          # seconds before cpu data is considered stale
  load_balancer: "cpu_aware"   # round_robin | cpu_aware | least_response | weighted

priorities:
  - path_prefix: "/premium"
    priority: 1
  - path_prefix: "/admin"
    priority: 1
  - path_prefix: "/api"
    priority: 5
  - path_prefix: "/"
    priority: 10               # catch-all batch

backends:
  - id: web1
    host: "web1:8000"
    container_name: "project_web1_1"
    capacity: 100
    protocol: http
  - id: web2
    host: "web2:8000"
    container_name: "project_web2_1"
    capacity: 100
    protocol: http
  - id: api1
    host: "api1:50051"
    container_name: "project_api1_1"
    capacity: 50
    protocol: grpc

circuit_breaker:
  failure_threshold: 3
  cooldown_seconds: 10
```

---

## Week by week

### Week 1 — Foundation
- [ ] Docker Compose: 2 web backends + proxy container
- [ ] Basic HTTP forwarding with Round Robin (approach A)
- [ ] `/ping` and `/healthz` on all backends
- [ ] Request IDs injected in gateway, basic trace logging

**Done when:** `curl localhost/` alternates between web1/web2, trace log shows the full path.

### Week 2 — Priority Queue + Circuit Breaker
- [ ] `asyncio.PriorityQueue` with N workers
- [ ] SLA tiers from config + `X-Priority` header override
- [ ] 429 when queue is full
- [ ] Circuit breaker per backend
- [ ] Request dedup
- [ ] Tracer hooked into queue + worker

**Done when:** Flood with mixed priorities — critical finishes first. Kill a backend — circuit breaker opens within 3 failures.

### Week 3 — Smart Load Balancing + Protocols
- [ ] `aiodocker` health loop — approach B (CPU aware)
- [ ] Approach C (active probing) and D (weighted score)
- [ ] Load balancer switchable via config
- [ ] Stale data fallback to round robin
- [ ] WebSocket support in gateway
- [ ] gRPC backend + HTTP↔gRPC translation
- [ ] Hot config reload (`POST /admin/reload`)

**Done when:** Can switch load balancer in config. Kill a backend — proxy detects it via docker stats, stops routing.

### Week 4 — Benchmark + Polish
- [ ] Run locust for all 4 approaches, save results to `benchmark/results/`
- [ ] Build `/dashboard` with live metrics
- [ ] `GET /trace/<id>` endpoint
- [ ] Stress test + fix anything broken
- [ ] Docker Compose cleanup (resource limits, restart policies)
- [ ] Prep demo scenarios below

**Done when:** Benchmark comparison table is ready. Live trace demo works cleanly.

---

## Demo scenarios

1. **Priority** — queue 100 batch requests, inject 10 critical ones mid-queue. Show traces proving critical finished first despite arriving later.

2. **Load balancing benchmark** — run locust with approach A (round robin) then approach B (CPU aware) side by side. Show the latency/throughput difference in a table.

3. **Backend failure** — kill web1 mid-traffic. Dashboard goes red, circuit breaker opens, traffic shifts automatically. Restart web1 — it comes back into rotation.

4. **Request trace** — call `GET /trace/<id>` and show the full timeline: gateway → queue wait → worker pickup → balancer decision → backend → response. Makes the priority queue visible.

5. **Hot reload** — add web3 to `config.yaml`, call `POST /admin/reload`, immediately see it appear in the dashboard and receive traffic.

---

## Things to say in the presentation

- **Why we benchmarked 4 approaches** — we wanted to understand the tradeoffs, not just pick one blindly. The benchmark is the proof that we actually tried them.
- **Why docker stats instead of self-reported health** — we observe backends from outside. More robust, works even if a backend is partially broken. Same pattern used by Portainer and Traefik.
- **Why health loop is decoupled from routing** — routing reads `backend.cpu`, never calls the health loop. Routing never blocks on a health check. Clean separation.
- **Why request tracing** — makes every design decision demonstrable with real data. Not just "it works", but "here's the proof it works".
- **Why Python asyncio over Nginx/HAProxy** — we could put Nginx in front for TLS, but the core logic needs to be custom. That's exactly how production stacks work — the right tool for each layer.
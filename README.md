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
┌─────────────────────────────────────────────────────┐
│                      CLIENTS                        │
│              HTTP      WebSocket      gRPC           │
└────────────────────────┬────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│                      PROXY                          │
│      X-Request-ID · X-Forwarded-For · Round Robin   │
│         Priority Queue · Circuit Breaker            │
└────────┬──────────────┬──────────────┬──────────────┘
         │              │              │
         ▼              ▼              ▼
    ┌─────────┐    ┌─────────┐   ┌─────────┐
    │  web1   │    │  web2   │   │ worker1 │  ...
    │ :8000   │    │ :8000   │   │  :8000  │
    └─────────┘    └─────────┘   └─────────┘

         /ping  /healthz  on every container
```

---

## Stack

| Thing | Tool |
|---|---|
| Proxy | Python `asyncio` + `aiohttp` |
| Backends | Python `FastAPI` |
| Container stats | `aiodocker` (reads CPU/mem from Docker API) |
| Internal protocol | `grpcio` for gRPC backends |
| Load testing | `locust` |
| Containers | Docker + Docker Compose |

**Why not Nginx/HAProxy?** They can't do a custom priority queue without Lua scripting nightmares. Python asyncio maps 1:1 to what the brief asks for. We could put Nginx in front just for TLS — worth mentioning in the presentation as that's how real stacks work.

---

## Project Structure

```
ProjetoRS/
├── src/
│   └── main.py          # dummy backend
├── proxy/
│   ├── main.py          # proxy core
│   └── Dockerfile
├── logs/                # proxy logs, rotates at 2MB
├── docker-compose.yml
└── Dockerfile
```

---

## Components

### Backends (`src/main.py`)

Two instances of the same FastAPI app (`web1`, `web2`) that pretend to do work. Each one:
- Accepts any HTTP request and sleeps 50–200ms to simulate real work
- Tracks active connections via middleware
- Exposes `/ping` to check if the container is alive
- Exposes `/healthz` to report load — active connections, capacity, load ratio and container ID

Backends are not exposed to the outside — only the proxy talks to them. Ports `8001` and `8002` are open for direct debugging only.

---

### Proxy (`proxy/main.py`)

Single `aiohttp` server that sits in front of the backends. Currently:
- Assigns a short unique `X-Request-ID` to every request
- Forwards `X-Forwarded-For` with the client IP
- Picks the next container via hand-rolled round robin (counter + modulo, no external libs)
- Forwards the request and returns the response
- Logs every step to `logs/main.py.log`

Round robin is hand-rolled intentionally — `itertools.cycle` hides the logic, a counter makes it explicit and easier to explain.

---

### Load Balancer — the interesting part

We implement 4 approaches, run them under the same load, and compare results. The point is to show we tried different things and understand the tradeoffs.

**Approach A — Round Robin** — already done. Blind rotation, ignores server state. Used as the baseline and fallback.

**Approach B — CPU aware via docker stats** — background loop polls every 5s using `aiodocker`. Routes to the least CPU loaded container. Falls back to round robin if data is stale.

**Approach C — Active probing** — sends a cheap `/ping` to each container before routing, picks the fastest responder. Better for bursty traffic, worse under sustained load because of the probe overhead.

**Approach D — Weighted score** — combines CPU, memory and active connections into a single score. Most nuanced but requires fresh docker stats data.

The proxy container needs access to the Docker socket for approaches B and D. This is the same pattern used by tools like Portainer and Traefik — worth mentioning in the presentation.

#### Benchmark plan
Run each approach with locust: 200 users, 60s, same backend setup. Collect P50/P95/P99 latency, throughput and error rate. Save results to `benchmark/results/`. Expected conclusion: CPU-aware wins under sustained load, active probing wins for bursty traffic, weighted is most fair but adds complexity.

---

### Request Tracing

Every request gets a unique ID at the proxy. Every step — received, queued, picked up by a worker, routed, responded — gets logged with that ID and a timestamp. This lets us reconstruct the full lifecycle of any request and makes the priority queue visible during the demo.

We also add a `GET /trace/<id>` endpoint that returns the full timeline for a given request ID.

---

### Priority Queue

Every request enters an `asyncio.PriorityQueue` before being forwarded. N workers drain it. Priority is assigned by path prefix and can be overridden with an `X-Priority` header.

| Tier | Priority | Routes |
|---|---|---|
| Critical | 1 | `/premium`, `/admin` |
| Standard | 5 | `/api/*` |
| Batch | 10 | `/export`, `/report`, everything else |

Lower number = served first. When the queue is full the proxy returns 429 instead of growing unbounded.

Also includes a circuit breaker per container — 3 failures in 10s marks it unhealthy and stops routing to it until it recovers.

---

### Observability

`GET /metrics` — queue depth, requests per container, CPU per container, circuit breaker state, P95 latency per route.

`GET /dashboard` — single HTML file served by the proxy, polls `/metrics` every second. Shows container load bars, queue depth, circuit breaker lights.

`POST /admin/reload` — re-reads config without restarting. Add a container, hit reload, it immediately gets traffic.

---

## Week by week

### Week 1 — Foundation 
- [] Docker Compose with 2 backends + proxy
- [] Basic HTTP forwarding
- [] Hand-rolled round robin
- [] `X-Request-ID` trace ID on every request
- [] `X-Forwarded-For` header forwarding
- [] `/ping` and `/healthz` on backends with container ID
- [] Logging to file
- [] /metrics endpoint (total requests, requests per container, errors per container, uptime)
- [] /status endpoint with live reachability check per container
- [] Startup health check before accepting traffic
- [] Log rotation (2MB cap, 5 backups)
- [] next_container skips unreachable containers
- [] 503 when no containers available
- [] query params preserved via rel_url

### Week 2 — Priority Queue + Circuit Breaker
- [ ] `asyncio.PriorityQueue` with N workers
- [ ] Priority assigned by path prefix
- [ ] `X-Priority` header override
- [ ] 429 when queue is full
- [ ] Circuit breaker per container
- [ ] Tracer logs at every step

### Week 3 — Smart Load Balancing + Protocols
- [ ] Background health loop via `aiodocker`
- [ ] Approach B — CPU aware
- [ ] Approach C — active probing
- [ ] Approach D — weighted score
- [ ] Stale data fallback to round robin
- [ ] WebSocket support
- [ ] gRPC backend (surface level)
- [ ] Hot config reload

### Week 4 — Benchmark + Polish
- [ ] Run locust for all 4 approaches, save results to `benchmark/results/`
- [ ] Live dashboard at `/dashboard`
- [ ] `/trace/<id>` endpoint
- [ ] Stress test + fix bottlenecks
- [ ] Demo prep

---

## Demo scenarios

1. **Priority** — flood with batch requests, inject critical ones mid-queue. Traces show critical finished first despite arriving later.
2. **Load balancing benchmark** — run locust with round robin vs CPU aware, show the latency/throughput difference in a table.
3. **Backend failure** — kill web1 mid-traffic, circuit breaker opens, traffic shifts to web2 automatically. Restart it — comes back into rotation.
4. **Request trace** — hit `/trace/<id>` and show the full timeline of one request through the system.
5. **Hot reload** — add web3 to config, call `/admin/reload`, immediately see it in the dashboard receiving traffic.

---

## Things to say in the presentation

- **Why we benchmarked 4 load balancing approaches** — to understand the tradeoffs, not just pick one blindly. The benchmark is the proof we actually tried them.
- **Why docker stats instead of self-reported health** — we observe containers from outside. More robust, works even if a backend is partially broken. Same pattern as Portainer and Traefik.
- **Why health loop is decoupled from routing** — routing reads the last known CPU value, never blocks waiting for a health check. Clean separation.
- **Why hand-rolled round robin** — simple, no dependencies, easy to reason about. Makes the logic explicit.
- **Why request tracing from day one** — makes every decision demonstrable with real data. Not just "it works" but here's the proof.
- **Why Python asyncio over Nginx/HAProxy** — right tool for the job. Nginx is great for TLS, not for custom priority queues.

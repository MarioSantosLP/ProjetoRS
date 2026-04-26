# Reverse Proxy & Load Balancer — Project Notes

> Tema 1 – Universidade de Aveiro  
> Internal doc — not a deliverable

---

## What we're building

An async reverse proxy in Python that:
- Receives HTTP requests and forwards them to Docker containers
- Uses round robin to distribute requests across containers
- Uses an `asyncio.PriorityQueue` to process requests by priority (coming week 2)
- Routes intelligently based on real container CPU usage via docker stats (coming week 3)
- Traces every request end-to-end so we can see exactly what the system is doing
- Benchmarks multiple load balancing approaches and compares them (coming week 3)

---

## Architecture

```
Clients (HTTP)
       │
       ▼
┌─────────────┐
│    Proxy    │  ← assigns request ID, forwards X-Forwarded-For, round robins
└──────┬──────┘
       │
  ┌────┴────┐
[web1]    [web2]
```

Will grow to include priority queue and smarter load balancing in later weeks.

---

## Stack

| Thing | Tool |
|---|---|
| Proxy | Python `asyncio` + `aiohttp` |
| Backends | Python `FastAPI` |
| Containers | Docker + Docker Compose |
| Load testing | `locust` (coming week 4) |
| Container stats | `aiodocker` (coming week 3) |

**Why not Nginx/HAProxy?** They can't do a custom priority queue without Lua scripting. Python asyncio maps directly to what the brief asks for. Could put Nginx in front for TLS — worth mentioning in the presentation as that's how real stacks work.

---

## Project Structure

```
ProjetoRS/
├── src/
│   └── main.py          # dummy backend — pretends to do work, returns JSON
├── proxy/
│   ├── main.py          # proxy — round robin, trace IDs, forwarding
│   └── Dockerfile
├── logs/
│   └── main.py.log      # proxy logs land here
├── docker-compose.yml
└── Dockerfile           # backend dockerfile
```

---

## What's built — Week 1 ✅

### Dummy backends (`src/main.py`)

Two instances of the same FastAPI app (`web1`, `web2`) that:
- Accept any HTTP request
- Sleep 50–200ms to simulate real work
- Return which service handled it and what path was hit
- Track active connections via middleware
- Expose `/ping` to check if alive
- Expose `/healthz` to check load (active connections / capacity)
- Include their container ID in responses so you can tell them apart

Backends are not exposed to the outside world — only the proxy talks to them. But ports `8001` and `8002` are exposed for direct debugging.

### Proxy (`proxy/main.py`)

A single `aiohttp` server that:
- Receives every incoming HTTP request
- Assigns a short unique `X-Request-ID` to it
- Forwards `X-Forwarded-For` with the client IP
- Picks the next container via round robin (hand-rolled with a counter + modulo, no external libs)
- Forwards the request to that container
- Returns the response
- Logs every step to `logs/main.py.log`

Round robin implementation:
```python
_rr_index = 0

def next_container() -> str:
    global _rr_index
    container = CONTAINERS[_rr_index % len(CONTAINERS)]
    _rr_index += 1
    return container
```

### Logging

All proxy activity logs to `logs/main.py.log` via Python's `logging` module. The `logs/` folder is mounted as a Docker volume so logs persist on your machine.

`aiohttp`'s own access log is silenced — only our logs appear.

---

## docker-compose.yml

```yaml
services:
  web1:
    build: .
    environment:
      - SERVICE_NAME=web1
    ports:
      - "8001:8000"

  web2:
    build: .
    environment:
      - SERVICE_NAME=web2
    ports:
      - "8002:8000"

  proxy:
    build: ./proxy
    ports:
      - "8080:8080"
    volumes:
      - ./logs:/app/logs
    depends_on:
      - web1
      - web2
```

---

## How to run

```bash
docker compose up --build
```

### Test round robin through proxy
```bash
curl http://localhost:8080/hello
curl http://localhost:8080/hello
curl http://localhost:8080/hello
```
Should alternate between web1 and web2 in the response.

### Test backends directly
```bash
curl http://localhost:8001/ping      # always web1
curl http://localhost:8002/ping      # always web2
curl http://localhost:8001/healthz
curl http://localhost:8002/healthz
```

### Test concurrent connections
```bash
curl http://localhost:8002/healthz &
curl http://localhost:8002/healthz &
curl http://localhost:8002/healthz &
```
`active_connections` will show > 1 when requests overlap.

### Watch proxy logs
```bash
docker compose logs -f proxy
```

---

## Week by week plan

### Week 1 — Foundation ✅
- [x] Docker Compose with 2 backends + proxy
- [x] Basic HTTP forwarding
- [x] Hand-rolled round robin
- [x] `X-Request-ID` trace ID on every request
- [x] `X-Forwarded-For` header forwarding
- [x] `/ping` and `/healthz` on backends with container ID
- [x] Logging to file

### Week 2 — Priority Queue + Circuit Breaker
- [ ] `asyncio.PriorityQueue` with N workers
- [ ] Priority assigned by path prefix (`/premium`=1, `/api`=5, everything else=10)
- [ ] `X-Priority` header override
- [ ] 429 when queue is full
- [ ] Circuit breaker — 3 failures in 10s → stop routing to that backend
- [ ] Tracer logs at every step (queued, dequeued, routed, responded)

### Week 3 — Smart Load Balancing
- [ ] Background health loop using `aiodocker` (reads real CPU from docker stats)
- [ ] Approach A — round robin (already done, used as fallback)
- [ ] Approach B — CPU aware (route to least loaded container)
- [ ] Approach C — active probing (ping each container, pick fastest)
- [ ] Approach D — weighted score (CPU + memory + connections)
- [ ] Stale data fallback to round robin
- [ ] gRPC backend (surface level)
- [ ] WebSocket support

### Week 4 — Benchmark + Polish
- [ ] Run locust for all 4 load balancing approaches, save results
- [ ] Live dashboard at `/dashboard`
- [ ] `/trace/<id>` endpoint — returns full timeline of a request
- [ ] Stress test + fix bottlenecks
- [ ] Demo prep

---

## Demo scenarios (planned)

1. **Priority** — flood with batch requests, inject critical ones mid-queue. Traces show critical finished first.
2. **Load balancing benchmark** — run locust with round robin vs CPU aware, show the difference in a table.
3. **Backend failure** — kill web1 mid-traffic, circuit breaker opens, traffic shifts to web2 automatically.
4. **Request trace** — hit `/trace/<id>` and show the full timeline of one request through the system.

---

## Things to say in the presentation

- **Why we benchmarked 4 load balancing approaches** — to understand the tradeoffs, not just pick one blindly. The benchmark is the proof we actually tried them.
- **Why docker stats instead of self-reported health** — we observe containers from outside. More robust, works even if a backend is partially broken.
- **Why hand-rolled round robin** — simple, no dependencies, easy to reason about. `itertools.cycle` hides the logic, a counter makes it explicit.
- **Why request tracing from day one** — makes every decision demonstrable. Not just "it works" but "here's the proof".
- **Why Python asyncio over Nginx/HAProxy** — right tool for the job. Nginx is great for TLS, not for custom priority queues.
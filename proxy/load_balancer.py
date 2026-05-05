import logging
import time
import asyncio
import aiodocker

from aiohttp import ClientSession, ClientTimeout

log = logging.getLogger("load_balancer")

CONTAINERS = [
    "http://web1:8000",
    "http://web2:8000",
]

DOCKER_NAMES = {
    "http://web1:8000": "projetors-web1-1",
    "http://web2:8000": "projetors-web2-1",
}

_rr_index = 0

def round_robin() -> str:
    global _rr_index
    container = CONTAINERS[_rr_index % len(CONTAINERS)]
    _rr_index += 1
    return container


#store container stats
container_stats: dict[str, dict] = {
    c: {
        "cpu": 0.0,
        "mem": 0.0,
        "healthy": True,
        "last_seen": 0.0,
    }
    for c in CONTAINERS
}

probe_stats: dict[str, dict] = {
    c: {
        "healthy": False,
        "latency_ms": None,
        "last_seen": 0.0,
    }
    for c in CONTAINERS
}

_active_connection: dict[str, int] = {c: 0 for c in CONTAINERS}

def conn_acquired(container: str) -> None:
    _active_connection[container] += 1

def conn_released(container: str) -> None:
    if _active_connection[container] > 0:
        _active_connection[container] -= 1


PROBE_TIMEOUT = 1
PROBE_INTERVAL = 2
PROBE_STALE = 6

#define weight of each contst

W_CPU = 0.4
W_MEM = 0.3
W_CONN = 0.3

MAX_CONN = 100

WEIGHTED_STATS_STALE = 15


async def cpu_aware() -> str | None:
    now =time.monotonic() #monotonic is better than time for time interval

    fresh = [
        c for c in CONTAINERS
        if container_stats[c]["healthy"]
        and now - container_stats[c]["last_seen"] < 15 #stats are fresh if seen in the last 15s
    ]

    if not fresh:
        log.warning("No healthy containers with fresh stats, falling back to round robin")
        return round_robin()
    
    return min(fresh, key=lambda c: container_stats[c]["cpu"]) #get cont with lowest cpu usage

async def probe_container(session: ClientSession, container: str) ->None:
    start = time.monotonic()
    try:
        async with session.get(
            f"{container}/ping",
            timeout=ClientTimeout(total=PROBE_TIMEOUT)
        
        ) as resp:
            healthy = resp.status == 200
            latency_ms = round((time.monotonic() - start) * 1000, 2)
            probe_stats[container]["latency_ms"] = latency_ms
            probe_stats[container]["healthy"] = healthy
            probe_stats[container]["last_seen"] = time.monotonic()
    except Exception as e:  
        log.warning(f"Probe failed for {container}: {e}")
        probe_stats[container]["healthy"] = False
        probe_stats[container]["latency_ms"] = None
        probe_stats[container]["last_seen"] = time.monotonic()

async def active_probe(session: ClientSession) -> str | None:
    if session is not None:
        await asyncio.gather(*(probe_container(session, c) for c in CONTAINERS))
    now = time.monotonic()
    fresh= [c for c in CONTAINERS
         if probe_stats[c]["healthy"] and probe_stats[c]["latency_ms"] is not None 
         and now - probe_stats[c]["last_seen"] < PROBE_STALE]
        
    if not fresh:
        log.warning("No healthy containers with fresh probe data")
        return round_robin()
        
    return min(fresh, key=lambda c: probe_stats[c]["latency_ms"]) #get cont with lowest latency

async def active_probe_loop(session: ClientSession) -> None:
    log.info("Starting active probing loop")

    while True:
        await asyncio.gather(
            *(probe_container(session, container) for container in CONTAINERS)
        )

        await asyncio.sleep(PROBE_INTERVAL)


async def weighted_stats() -> str | None:
    now = time.monotonic()

    fresh = [
        c for c in CONTAINERS
        if container_stats[c]["healthy"]
        and now - container_stats[c]["last_seen"] < WEIGHTED_STATS_STALE
    ]

    if not fresh:
        log.warning("No healthy containers with fresh stats for weighted selection, falling back to round robin")
        return round_robin()
    

    def score(container:str) -> float:
        cpu_score = container_stats[container]["cpu"] / 100
        mem_score = container_stats[container]["mem"] / 100
        conn_score = min(_active_connection[container] / MAX_CONN, 1.0)

        return W_CPU * cpu_score + W_MEM * mem_score + W_CONN * conn_score
    
    scores = {c: score(c) for c in fresh}
    best = min(scores, key=lambda c: scores[c]) #get cont with lowest score
    return best

    


async def health_loop()-> None:


    log.info("Starting health loop")

    async with aiodocker.Docker() as docker:
        while True:
            for container in CONTAINERS:
                try:
                    name = DOCKER_NAMES[container]
                    c = await docker.containers.get(name)
                    stats = await c.stats(stream=False) #get container stats
                    stats = stats[0] #only need first one from the dict
                    #print(stats) 

                    # CPU % calculation  Docker's own CLI source:
                    # https://github.com/moby/moby/blob/eb131c5383db8cac633919f82abad86c99bffbe5/cli/command/container/stats_helpers.go#L175
                    # cpu_delta  = current total CPU usage - previous(precpu) total CPU usage
                    # sys_delta  = current system CPU usage - previous system CPU usage
                    # cpu%       = (cpu_delta / sys_delta) * num_cpus * 100

                    #precpu stats are from the last snapshot (docker always gives us 2 snapshots)
                    #total_usage is the time the cpu has been used in ns
                    cpu_diff = (
                        stats["cpu_stats"]["cpu_usage"]["total_usage"]
                        - stats["precpu_stats"]["cpu_usage"]["total_usage"]
                    )


                    system_diff = (
                        stats["cpu_stats"]["system_cpu_usage"]
                        - stats["precpu_stats"]["system_cpu_usage"]
                    )

                    num_cpus = stats["cpu_stats"].get("online_cpus", 1)
                    cpu = (cpu_diff / system_diff) * num_cpus * 100 if system_diff > 0 else 0.0 

                    container_stats[container]["cpu"] = round(cpu, 2)
                    container_stats[container]["healthy"] = True
                    container_stats[container]["last_seen"] = time.monotonic()

                    #mem calc

                    mem_stats = stats["memory_stats"]
                    mem_usage  = mem_stats["usage"] - mem_stats.get("stats", {}).get("cache", 0)
                    mem_limit  = mem_stats.get("limit", 1)  # bytes; avoid /0
                    mem = (mem_usage / mem_limit) * 100 if mem_limit > 0 else 0.0
    
                except Exception as e:
                    log.warning(f"Failed to get stats for {container}: {e}")
                    container_stats[container]["healthy"] = False

            await asyncio.sleep(5) #wait 5s before next check

if __name__ == "__main__":
    asyncio.run(health_loop())

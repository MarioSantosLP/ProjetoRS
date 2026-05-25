import logging
import time
import asyncio
import aiodocker
import json

from aiohttp import ClientSession, ClientTimeout

log = logging.getLogger("load_balancer")

CONFIG_PATH = "config/config.json"

#idea:hot reload saw it in nginx
#and wanted to see if we can implement 
#making it so that we can update the container list and roles without restarting the load balancer

#to be updated by reload_config
CONTAINERS : list[str] = []
DOCKER_NAMES : dict[str, str] = {}
CONTAINER_ROLES : dict[str, str] = {}
DOCKER_HOSTS : dict[str, str] = {}  # url -> docker host (local socket or remote TCP)
GRPC_URLS : dict[str, str] = {}     # url -> grpc address (host:port)

container_stats : dict[str, dict] = {}
probe_stats : dict[str, dict] = {}
_active_connection : dict[str, int] = {}
DISABLED_CONTAINERS : set[str] = set()

_rr_index = 0


def _default_container_stats() -> dict:
    return {
        "cpu": 0.0,
        "mem": 0.0,
        "healthy": True,
        "last_seen": 0.0,
    }

def default_probe_stats() -> dict:
    return {
        "healthy": False,
        "latency_ms": None,
        "last_seen": 0.0,
    }

def load_config() -> None:
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    
    for entry in cfg["containers"]:

        url = entry["url"]
        DISABLED_CONTAINERS.discard(url)  # in case it was previously disabled

        if url not in CONTAINERS:
            CONTAINERS.append(url)
            DOCKER_NAMES[url] = entry["docker_name"]
            CONTAINER_ROLES[url] = entry.get("role", "general")
            # if no docker_host is set, fall back to local socket (single machine setup)
            DOCKER_HOSTS[url] = entry.get("docker_host", "unix:///var/run/docker.sock")
            # if no grpc_url is set, derive it from the container url (local containers use internal DNS + port 50051)
            GRPC_URLS[url] = entry.get("grpc_url") or (url.replace("http://", "").split(":")[0] + ":50051")
            container_stats.setdefault(url, _default_container_stats())
            probe_stats.setdefault(url, default_probe_stats())
            _active_connection.setdefault(url, 0)
        
    log.info(f"Config loaded the conts {CONTAINERS}")


def reload_config() -> tuple[list[str], list[str]]:

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    
    new_urls = {entry["url"] for entry in cfg["containers"]}
    old_urls = set(CONTAINERS)

    added = [url for url in new_urls if url not in old_urls]
    removed = [url for url in old_urls if url not in new_urls]

    #add conts
    for entry in cfg["containers"]:
        url = entry["url"]
        DISABLED_CONTAINERS.discard(url) #fix bug where stays disabled
        DOCKER_NAMES[url] = entry["docker_name"]
        CONTAINER_ROLES[url] = entry.get("role", "general")
        # if no docker_host is set, fall back to local socket (single machine setup)
        DOCKER_HOSTS[url] = entry.get("docker_host", "unix:///var/run/docker.sock")
        GRPC_URLS[url] = entry.get("grpc_url") or (url.replace("http://", "").split(":")[0] + ":50051")
        if url not in CONTAINERS:
            CONTAINERS.append(url)
        container_stats.setdefault(url, _default_container_stats()) #could use an if but found this cleaver method
        probe_stats.setdefault(url, default_probe_stats())
        _active_connection.setdefault(url, 0)

    for url in removed:
        if url in CONTAINERS:
            CONTAINERS.remove(url)

        DISABLED_CONTAINERS.add(url)
        DOCKER_NAMES.pop(url, None)
        CONTAINER_ROLES.pop(url, None)
        DOCKER_HOSTS.pop(url, None)
        GRPC_URLS.pop(url, None)
        container_stats.pop(url, None)
        probe_stats.pop(url, None)
        _active_connection.pop(url, None)

        if url in container_stats:
            container_stats[url]["healthy"] = False

        if url in probe_stats:
            probe_stats[url]["healthy"] = False
            probe_stats[url]["latency_ms"] = None
        
    log.info(f"Config reloaded — added: {added}, removed: {removed}, active: {CONTAINERS}")
    return added, removed



#helper to check conts
def _enabled(pool: list[str] | None = None) -> list[str]:
    candidates = pool if pool is not None else CONTAINERS
    return [c for c in candidates if c not in DISABLED_CONTAINERS]


def round_robin(pool: list[str] | None = None) -> str:
    global _rr_index
    candidates = _enabled(pool)
    if not candidates:
        return None

    container = candidates[_rr_index % len(candidates)] #0 % 3 cont1
    _rr_index += 1
    return container


def conn_acquired(container: str) -> None:
    _active_connection[container] += 1
    log.debug(f"conn_acquired {container} -> active={_active_connection[container]}")

def conn_released(container: str) -> None:
    if _active_connection[container] > 0:
        _active_connection[container] -= 1
        log.debug(f"conn_released {container} -> active={_active_connection[container]}")


PROBE_TIMEOUT = 1
PROBE_INTERVAL = 2      
PROBE_STALE = 6

W_CPU = 0.4
W_MEM = 0.3
W_CONN = 0.3

MAX_CONN = 100

WEIGHTED_STATS_STALE = 15


async def cpu_aware(pool: list[str] | None = None) -> str | None:
    candidates = _enabled(pool)
    now = time.monotonic()

    fresh = [
        c for c in candidates
        if container_stats[c]["healthy"]
        and now - container_stats[c]["last_seen"] < WEIGHTED_STATS_STALE
    ]

    if not fresh:
        log.warning("No healthy containers with fresh stats, falling back to round robin")
        return round_robin(candidates)

    return min(fresh, key=lambda c: container_stats[c]["cpu"])

async def probe_container(session: ClientSession, container: str) -> None: #added guard for disabled conts
    if container in DISABLED_CONTAINERS:
        return

    probe_stats.setdefault(container, default_probe_stats())

    start = time.monotonic()

    try:
        async with session.get(
            f"{container}/ping",
            timeout=ClientTimeout(total=PROBE_TIMEOUT)
        ) as resp:
            healthy = resp.status == 200
            latency_ms = round((time.monotonic() - start) * 1000, 2)

            if container in DISABLED_CONTAINERS:
                return

            probe_stats[container]["latency_ms"] = latency_ms
            probe_stats[container]["healthy"] = healthy
            probe_stats[container]["last_seen"] = time.monotonic()

    except Exception as e:
        log.warning(f"Probe failed for {container}: {e}")

        if container in DISABLED_CONTAINERS:
            return

        probe_stats.setdefault(container, default_probe_stats())
        probe_stats[container]["healthy"] = False
        probe_stats[container]["latency_ms"] = None
        probe_stats[container]["last_seen"] = time.monotonic()

async def active_probe(session: ClientSession, pool: list[str] | None = None) -> str | None:
    candidates = _enabled(pool)
    # Read from the cache kept fresh by active_probe_loop — no inline probing on the hot path
    now = time.monotonic()
    fresh = [
        c for c in candidates
        if probe_stats[c]["healthy"] and probe_stats[c]["latency_ms"] is not None
        and now - probe_stats[c]["last_seen"] < PROBE_STALE
    ]

    if not fresh:
        log.warning("No healthy containers with fresh probe data")
        return round_robin(candidates)

    return min(fresh, key=lambda c: probe_stats[c]["latency_ms"])

async def active_probe_loop(session: ClientSession) -> None:
    log.info("Starting active probing loop")

    while True:
        targets = list(CONTAINERS)  # make a copy to avoid issues if CONTAINERS changes during hot reload
        await asyncio.gather(
            *(probe_container(session, container) for container in targets)
        )

        await asyncio.sleep(PROBE_INTERVAL)


async def weighted_stats(pool: list[str] | None = None) -> str | None:
    candidates = _enabled(pool)
    now = time.monotonic()

    fresh = [
        c for c in candidates
        if container_stats[c]["healthy"]
        and now - container_stats[c]["last_seen"] < WEIGHTED_STATS_STALE
    ]

    if not fresh:
        log.warning("No healthy containers with fresh stats for weighted selection, falling back to round robin")
        return round_robin(candidates)

    def score(container: str) -> float:
        cpu_score = container_stats[container]["cpu"] / 100
        mem_score = container_stats[container]["mem"] / 100
        conn_score = min(_active_connection[container] / MAX_CONN, 1.0)
        return W_CPU * cpu_score + W_MEM * mem_score + W_CONN * conn_score

    return min(fresh, key=score)


def _candidates_for_workload(workload_type: str | None, exclude: set[str] | None = None) -> list[str]:
    exclude = exclude or set()
    active = [c for c in _enabled() if c not in exclude]

    if workload_type not in ("cpu", "memory"):
        return active

    matched = [c for c in active if CONTAINER_ROLES.get(c) == workload_type]
    if matched:
        return matched

    log.warning(f"No containers with role '{workload_type}', falling back to full pool")
    return active


async def pick_by_role(workload_type: str | None, algorithm: str, session=None, exclude: set[str] | None = None) -> str | None:
    
    pool = _candidates_for_workload(workload_type, exclude)

    if algorithm == "round_robin":
        result = round_robin(pool)
    elif algorithm == "cpu_aware":
        result = await cpu_aware(pool)
    elif algorithm == "active_probe":
        result = await active_probe(session, pool)
    elif algorithm == "weighted":
        result = await weighted_stats(pool)
    else:
        result = round_robin(pool)

    log.debug(f"pick_by_role workload={workload_type} algo={algorithm} → {result}")
    return result


async def health_loop() -> None:
    log.info("Starting health loop")

    while True:
        targets = list(CONTAINERS)  # make a copy to avoid issues if CONTAINERS changes during hot reload

        # group containers by their docker host so we open one connection per machine
        # containers without docker_host use the local socket
        host_groups: dict[str, list[str]] = {}
        for container in targets:
            host = DOCKER_HOSTS.get(container, "unix:///var/run/docker.sock")
            host_groups.setdefault(host, []).append(container)

        for docker_host, containers in host_groups.items():
            try:
                async with aiodocker.Docker(url=docker_host) as docker:
                    for container in containers:
                        try:
                            name = DOCKER_NAMES[container]
                            c = await docker.containers.get(name)
                            stats = await c.stats(stream=False) #get container stats
                            stats = stats[0] #only need first one from the dict

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
                            mem_usage = mem_stats["usage"] - mem_stats.get("stats", {}).get("cache", 0)
                            mem_limit = mem_stats.get("limit", 1)  # bytes; avoid /0
                            mem = (mem_usage / mem_limit) * 100 if mem_limit > 0 else 0.0
                            container_stats[container]["mem"] = round(mem, 2)

                        except Exception as e:
                            log.warning(f"Failed to get stats for {container}: {e}")
                            container_stats[container]["healthy"] = False

            except Exception as e:
                # entire machine is down — mark all its containers as unhealthy
                log.warning(f"Failed to connect to Docker at {docker_host}: {e}")
                for container in containers:
                    container_stats[container]["healthy"] = False

        await asyncio.sleep(5) #wait 5s before next check

if __name__ == "__main__":
    asyncio.run(health_loop())
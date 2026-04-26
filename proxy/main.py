import asyncio
import itertools
import logging
import uuid
import sys

from aiohttp import web, ClientSession, ClientTimeout

#Logging (ts + msg) method used in lab7_8
logging.basicConfig(
    filename=f"logs/{sys.argv[0]}.log",
    level=logging.DEBUG,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
log = logging.getLogger("proxy")

# the containers
CONTAINERS = [
    "http://web1:8000",
    "http://web2:8000",
]

# Round robin — itertools.cycle just loops through the list forever
_rr = itertools.cycle(CONTAINERS)

def next_container() -> str:
    return next(_rr)


async def handle(request: web.Request) -> web.Response:
    # 1. give ID to this request
    req_id = str(uuid.uuid4())[:8]

    # load balance the containers
    container = next_container()

    log.info(f"[{req_id}] {request.method} {request.path} → {container} (from {request.remote})")

    
    url = f"{container}{request.path}"

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
        log.error(f"[{req_id}] Failed to reach {container}: {e}")
        return web.Response(status=502, text="Container unavailable")


app = web.Application()
app.router.add_route("*", "/{path_info:.*}", handle)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8080)
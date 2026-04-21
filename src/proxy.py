import aiohttp
from aiohttp import web
from load_balancer import get_container, release_request_from_container

#how to make the decision on where to route the request?
#for now i have a logic first based on the host header
#then based on the content type header for api and web
#then based on the method and content type for worker

#Questions for teacher:
#1. How should i handle the routing logic?
#2. Load balancing can only be done with one algorithm, or can i have different algorithms for different containers?
#3 How to handle a situ where a container is down? health checks? retry logic?


async def foward_request(request, service, dcontainer):
    async with aiohttp.ClientSession() as session:
        async with session.request(
            method=request.method,
            url=f"{dcontainer}{request.path_qs}", #(qs =query string)so we get the query paramaters as well
            headers=request.headers,
            data= await request.read()
        ) as r:
            status = r.status
            headers = r.headers
            body = await r.read()
            await release_request_from_container(service, dcontainer) #release the container after the request is done
            return web.Response(status=status, headers=headers, body=body)
        


async def handler(request):
    host = request.headers.get("Host", "")
    x_forwarded = request.headers.get("X-Forwarded-For", "unknown")
    method = request.method
    path = request.path
    type = request.headers.get("Content-Type", "unknown")

    print(f"request from: {x_forwarded}")
    print(f"wants to reach: {host}")
    print(f"method: {method}")
    print(f"path: {path}")
    print(f"type: {type}")

    if "api" in host or "application/json" in type:
        service = "api"
    elif "web" in host or "text/html" in type:
        service = "web"
    elif "worker" in host or (method in ["POST", "PUT", "PATCH"] and "application/json" in type):
        service = "worker"
    else:
        service = "api"  # default service (ask teacher if it shouldnt just return an error instead)
    
    dcontainer = await get_container(service)
    print(f"routing to: {dcontainer}")
    return await foward_request(request, service, dcontainer)


app = web.Application()
app.router.add_route("*", "/{path_info:.*}", handler, name ="proxy") #need to catch all the routes

web.run_app(app, port=8080)
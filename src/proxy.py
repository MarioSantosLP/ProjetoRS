from aiohttp import web

async def handler(request):
    host = request.headers.get("Host", "")
    x_forwarded = request.headers.get("X-Forwarded-For", "unknown")

    print(f"request from: {x_forwarded}")
    print(f"wants to reach: {host}")

    if "api" in host:
        backend = "http://localhost:5001"
    elif "web" in host:
        backend = "http://localhost:5002"
    else:
        backend = "http://localhost:5000"

    print(f"routing to: {backend}")
    return web.Response(text=f"would forward to {backend}")

app = web.Application()
app.router.add_route("*", "/{path_info:.*}", handler)

web.run_app(app, port=8080)
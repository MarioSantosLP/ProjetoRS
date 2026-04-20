import asyncio
from aiohttp import web

async def handler(request):
    print(request.headers)
    return web.Response(text="hello")

app = web.Application()
app.router.add_route("*", "/{path_info:.*}", handler)

web.run_app(app, port=8080)
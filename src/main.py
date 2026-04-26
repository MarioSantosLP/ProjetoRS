import asyncio
import os
import random
from fastapi import FastAPI, Request

app = FastAPI()
NAME = os.getenv("SERVICE_NAME", "web")


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def handle(request: Request, path: str):
    await asyncio.sleep(random.uniform(0.05, 0.2))  # pretend to do work
    return {
        "service": NAME,
        "path": f"/{path}",
        "method": request.method,
    }
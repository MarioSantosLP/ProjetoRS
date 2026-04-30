import asyncio
import os
import random
import socket
from fastapi import FastAPI, Request

app = FastAPI()

NAME = os.getenv("SERVICE_NAME", "web")
CAPACITY = int(os.getenv("CAPACITY", "100"))
active_connections = 0


@app.middleware("http")
async def count_connections(request: Request, call_next):
    global active_connections
    active_connections += 1
    try:
        response = await call_next(request)
        return response
    finally:
        active_connections -= 1


@app.get("/ping") # simple endpoint to check if the service is alive
async def ping():
    return {
        "service": NAME,
        "container_id": socket.gethostname(), #this since we are using docker will return the container id
        "status": "ok",
    }


@app.get("/healthz") # endpoint to check the health of the service
async def healthz():
    return {
        "service": NAME,
        "container_id": socket.gethostname(),
        "status": "ok",
        "active_connections": active_connections, #shows min 1 because healthz is also a request
        "capacity": CAPACITY,
        "load": round(active_connections / CAPACITY, 3),
    }



@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def handle(request: Request, path: str):
    await asyncio.sleep(random.uniform(0.05, 0.2))
    return {
        "service": NAME,
        "path": f"/{path}",
        "method": request.method,
    }
import asyncio
import os
import random
import socket
from fastapi import FastAPI, Request

import json
import grpc
import sys

sys.path.insert(0, "/app")
import service_pb2
import service_pb2_grpc

app = FastAPI()

NAME = os.getenv("SERVICE_NAME", "web")
try: #fix copilot suggesting non-integer values for capacity
    CAPACITY = int(os.getenv("CAPACITY", "100"))
except ValueError:
    raise ValueError("CAPACITY must be a valid integer")

if CAPACITY <= 0:
    raise ValueError("CAPACITY must be greater than 0")
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

# --- gRPC ---

class WebServiceServicer(service_pb2_grpc.WebServiceServicer):
    async def HandleRequest(self, request, context):
        await asyncio.sleep(random.uniform(0.05, 0.2))
        response_data = {
            "service": NAME,
            "path": request.path,
            "method": request.method,
        }
        return service_pb2.HttpResponse(
            status=200,
            body=json.dumps(response_data).encode(),
            headers={"Content-Type": "application/json"},
        )

async def serve_grpc():
    server = grpc.aio.server()
    service_pb2_grpc.add_WebServiceServicer_to_server(WebServiceServicer(), server)
    server.add_insecure_port("[::]:50051")
    await server.start()
    await server.wait_for_termination()

@app.on_event("startup")
async def startup():
    asyncio.create_task(serve_grpc())
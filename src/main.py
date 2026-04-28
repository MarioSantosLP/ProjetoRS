import os
import socket
from fastapi import FastAPI

app = FastAPI()

NAME = os.getenv("SERVICE_NAME", "web")

@app.get("/ping")
async def ping():
    return {
        "service": NAME,
        "container_id": socket.gethostname(),
        "status": "ok",
    }

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def handle(path: str):
    return {
        "service": NAME,
        "path": f"/{path}",
    }

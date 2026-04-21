from fastapi import FastAPI
import asyncio
app = FastAPI()

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def catch_all(path: str):
    await asyncio.sleep(2)
    return {"container": "worker", "instance": 1}
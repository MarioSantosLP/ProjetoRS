FROM python:3.12-slim
WORKDIR /app
RUN pip install aiohttp grpcio grpcio-tools protobuf aiodocker --no-cache-dir
RUN mkdir -p logs
COPY protos/service.proto .
RUN python -m grpc_tools.protoc \
    -I. \
    --python_out=. \
    --grpc_python_out=. \
    service.proto
COPY main.py load_balancer.py priority_queue.py circuit.py ./
CMD ["python", "main.py"]
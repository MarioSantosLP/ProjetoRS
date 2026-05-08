FROM python:3.12-slim
WORKDIR /app
RUN pip install aiohttp grpcio protobuf --no-cache-dir
RUN mkdir -p logs
COPY main.py priority_queue.py circuit.py service_pb2.py service_pb2_grpc.py ./
CMD ["python", "main.py"]
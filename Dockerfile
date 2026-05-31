FROM python:3.12-slim
WORKDIR /app
RUN pip install fastapi uvicorn websockets grpcio grpcio-tools protobuf --no-cache-dir
COPY proxy/protos/service.proto .
RUN python -m grpc_tools.protoc \
    -I. \
    --python_out=. \
    --grpc_python_out=. \
    service.proto
RUN apt-get update && apt-get install -y iproute2 && rm -rf /var/lib/apt/lists/*
COPY src/main.py .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
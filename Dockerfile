FROM python:3.12-slim
WORKDIR /app
RUN pip install fastapi uvicorn grpcio grpcio-tools protobuf --no-cache-dir
COPY proxy/protos/service.proto .
RUN python -m grpc_tools.protoc \
    -I. \
    --python_out=. \
    --grpc_python_out=. \
    service.proto
COPY src/main.py .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
FROM python:3.12-slim
WORKDIR /app
RUN pip install fastapi uvicorn grpcio protobuf --no-cache-dir
COPY src/main.py .
COPY src/service_pb2.py .
COPY src/service_pb2_grpc.py .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
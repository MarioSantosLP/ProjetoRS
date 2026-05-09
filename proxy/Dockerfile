FROM python:3.12-slim
WORKDIR /app
RUN pip install aiohttp aiodocker --no-cache-dir
RUN mkdir -p logs
COPY main.py load_balancer.py ./
CMD ["python", "main.py"]
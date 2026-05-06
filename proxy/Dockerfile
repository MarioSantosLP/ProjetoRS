FROM python:3.12-slim
WORKDIR /app
RUN pip install aiohttp --no-cache-dir
RUN mkdir -p logs
COPY main.py priority_queue.py circuit.py ./
CMD ["python", "main.py"]
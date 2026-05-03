FROM python:3.12-slim
WORKDIR /app
RUN pip install aiohttp --no-cache-dir
RUN mkdir -p logs
COPY main.py .
CMD ["python", "main.py"]
# Nginx Reverse Proxy

## How to run

```bash
docker compose up --build
```

## How to test

Test round-robin load balancing — run multiple times and notice the service alternating between `web1` and `web2`:

```bash
curl http://localhost:8080/ping
```

Test any other route:

```bash
curl http://localhost:8080/hello
curl http://localhost:8080/anything
```

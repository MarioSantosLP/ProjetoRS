# Comandos apresentação RS

## basics:

```bash
curl -s http://localhost:8080/status | jq
curl -s http://localhost:8080/metrics | jq
curl -s http://localhost:8080/api/test | jq
curl -s http://localhost:8001/ping | jq
curl -s http://localhost:8001/healthz | jq
```

## Load Balancing commands:

### Metrics:

```bash
watch -n 2 'curl -s http://localhost:8080/metrics | jq -r "
  \"=== CPU/MEM ===\",
  (.container_stats | to_entries[] | \"\(.key | split(\":\")[0] | ltrimstr(\"http://\"))\t cpu:\(.value.cpu)%\t mem:\(.value.mem)%\t ok:\(.value.healthy)\"),
  \"=== PROBES ===\",
  (.probe_stats | to_entries[] | \"\(.key | split(\":\")[0] | ltrimstr(\"http://\"))\t ms:\(.value.latency_ms)\t ok:\(.value.healthy)\")
"'
```

### Ver conts:

```bash
for i in $(seq 1 20); do curl -s http://localhost:8080/api/test | jq -r .service; done
```

```bash
for i in {1..20}; do
  curl -s http://localhost:8080/api/test | jq -r '.service'
done | sort | uniq -c
```

### Burns:

```bash
curl -s "http://localhost:8001/burn/cpu?duration=20" | jq
curl -s "http://localhost:8002/burn/memory?size_mb=280&duration=20" | jq
```

## Active probe

aumentar a latency com o traffic shaping mandar o professor ver para o pc ig?

### clean before:

```bash
docker exec -u root proxy-web1 tc qdisc del dev eth0 root 2>/dev/null || true
```

```bash
docker exec -u root proxy-web1 tc qdisc add dev eth0 root netem delay 100ms
```

### clean:

```bash
docker exec -u root proxy-web1 tc qdisc del dev eth0 root
```

## Concorrência

```bash
ab -n 300 -c 30 http://localhost:8080/api/test
```

## Hot reload:

```bash
curl -s -X POST http://localhost:8080/admin/reload | jq
```

## Workload type

```bash
for i in {1..30}; do
  curl -s -H "X-Workload-Type: cpu" http://localhost:8080/api/test | jq -r '.service'
done | sort | uniq -c
```

```bash
for i in {1..30}; do
  curl -s -H "X-Workload-Type: memory" http://localhost:8080/api/test | jq -r '.service'
done | sort | uniq -c
```

## Traffic shaping:

### basic conn

ip do not forget

```bash
curl http://192.168.1.108:8003/ping
curl http://192.168.1.108:8004/ping
```

### rr

```bash
for i in {1..4}; do curl -s http://localhost:8080/test; echo; done
```

### latency

```bash
sudo tc qdisc add dev wlp1s0 root netem delay 100ms
for i in {1..20}; do curl -s http://localhost:8080/test; echo; done
curl -s http://localhost:8080/metrics
sudo tc qdisc del dev wlp1s0 root
```

### Packet loss

```bash
sudo tc qdisc add dev wlp1s0 root netem loss 10%
for i in {1..20}; do curl -s http://localhost:8080/test; echo; done
curl -s http://localhost:8080/metrics
sudo tc qdisc del dev wlp1s0 root
```

### Test delay and packet loss

```bash
sudo tc qdisc add dev wlp1s0 root netem delay 100ms loss 5%
for i in {1..20}; do curl -s http://localhost:8080/test; echo; done
curl -s http://localhost:8080/metrics
sudo tc qdisc del dev wlp1s0 root
```
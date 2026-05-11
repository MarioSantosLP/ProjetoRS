#!/bin/bash

# ============================================================
# Load Balancer Test Script
# Change ALGORITHM to test different load balancing strategies
# Options: round_robin | cpu_aware | active_probe | weighted
# ============================================================

ALGORITHM="active_probe"
REQUESTS=10

# --- Update algorithm in proxy config ---
echo "=== Building com algoritmo: $ALGORITHM ==="
sed -i "s/LOAD_BALANCER = .*/LOAD_BALANCER = \"$ALGORITHM\"/" proxy/main.py

# --- Stop containers ---
sudo docker-compose down

# --- Build and start containers ---
docker-compose build --no-cache proxy && docker-compose up -d

# --- Wait for proxy to be ready ---
echo "Aguardando proxy arrancar..."
until curl -s http://localhost:8080/status > /dev/null 2>&1
do
    echo "  ainda a arrancar..."
    sleep 1
done
echo "Proxy pronto!"

# --- Send test requests ---
echo ""
echo "=== $REQUESTS pedidos ==="
for i in $(seq 1 $REQUESTS)
do
    response=$(curl -s http://localhost:8080/api/test)
    service=$(echo $response | python3 -c "import sys,json; print(json.load(sys.stdin)['service'])")
    echo "  Pedido $i → $service"
done

# --- Logs ---
echo ""
echo "=== Logs finais ==="
sleep 3
docker exec projetors-proxy-1 tail -20 logs/main.py.log
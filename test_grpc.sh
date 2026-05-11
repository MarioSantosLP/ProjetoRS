#!/bin/bash

# ============================================================
# gRPC Test Script
# Tests that the proxy is communicating with backends via gRPC
# ============================================================

REQUESTS=10

# --- Stop containers ---
sudo docker-compose down

# --- Build and start containers ---
docker-compose up --build -d

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
echo "=== $REQUESTS pedidos via proxy (HTTP -> gRPC) ==="
for i in $(seq 1 $REQUESTS)
do
    response=$(curl -s http://localhost:8080/api/test)
    service=$(echo $response | python3 -c "import sys,json; print(json.load(sys.stdin)['service'])")
    echo "  Pedido $i → $service"
done

# --- Check logs for gRPC confirmation ---
echo ""
echo "=== Confirmação gRPC nos logs ==="
sleep 3
docker exec projetors-proxy-1 tail -20 logs/main.py.log | grep -E "gRPC|_cygrpc"
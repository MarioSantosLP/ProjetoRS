#!/bin/bash

ALGORITHM="round_robin"  # mudem aqui: round_robin | cpu_aware | active_probe | weighted
REQUESTS=10

echo "=== Building com algoritmo: $ALGORITHM ==="
sed -i "s/LOAD_BALANCER = .*/LOAD_BALANCER = \"$ALGORITHM\"/" proxy/main.py
docker-compose up --build -d

echo "Aguardando proxy arrancar..."
until curl -s http://localhost:8080/status > /dev/null 2>&1; do
    echo "  ainda a arrancar..."
    sleep 1
done
echo "Proxy pronto!"

echo ""
echo "=== $REQUESTS pedidos ==="
for i in $(seq 1 $REQUESTS); do
    response=$(curl -s http://localhost:8080/api/test)
    service=$(echo $response | python3 -c "import sys,json; print(json.load(sys.stdin)['service'])")
    echo "  Pedido $i → $service"
done

echo ""
echo "=== Logs finais ==="
docker exec projetors-proxy-1 tail -15 logs/main.py.log
#Round Robin Load Balancer Implementation
import asyncio


class Container:
    def __init__(self, url, max_requests):
        self.url = url
        self.active = True
        self.total_requests = 0
        self.current_requests = 0
        self.max_requests = max_requests

docker_containers= {
    "api": [Container(url="http://localhost:5001", max_requests =10), 
            Container(url="http://localhost:5002", max_requests =10)],

    "web": [Container(url="http://localhost:5003", max_requests =10), 
            Container(url="http://localhost:5004", max_requests =10)],

    "worker": [Container(url="http://localhost:5005", max_requests =4),  #worker is sloower so less requests
               Container(url="http://localhost:5006", max_requests =4)]
}

counter = {
    "api": 0,
    "web": 0,
    "worker": 0
}

lock = asyncio.Lock()

async def get_container(service):
    async with lock:
        active_containers= [
            cont for cont in docker_containers[service]
            if cont.active and cont.current_requests < cont.max_requests
        ]
        if not active_containers:
            raise Exception(f"No active containers for {service}")
        
        idx = counter[service] % len(active_containers)
        counter[service] += 1

        selected_container = active_containers[idx]
        selected_container.current_requests += 1
        selected_container.total_requests += 1

        print(f"Selected container for {service}: {selected_container.url} (current: {selected_container.current_requests}, total: {selected_container.total_requests})")
        return selected_container.url
    
async def release_request_from_container(service, container):
    async with lock:
        for cont in docker_containers[service]:
            if cont.url == container and cont.current_requests > 0 : #to confirm it doesnt go negative
                cont.current_requests -= 1
                print(f"Released container for {service}: {cont.url} (current: {cont.current_requests}, total: {cont.total_requests})")
                break



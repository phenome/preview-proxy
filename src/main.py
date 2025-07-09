import docker
import os
import time
import socket
from flask import Flask, request, Response
import requests
from threading import Thread, Lock

# --- Configuration from Environment Variables ---
# The base URL path for the proxy. e.g., "preview" -> /preview/<tag>
PROXY_BASE_PATH = os.environ.get('PROXY_BASE_PATH', '').strip('/')

# The base Docker image. e.g., "my-org/my-app" -> my-org/my-app:<tag>
PROXY_BASE_IMAGE = os.environ.get('PROXY_BASE_IMAGE', '')

# The port the target containers are expected to listen on.
PROXY_TARGET_PORT = int(os.environ.get('PROXY_TARGET_PORT', 80))

# How long a container can be idle before being stopped.
CONTAINER_IDLE_TIMEOUT = int(os.environ.get('CONTAINER_IDLE_TIMEOUT', 300)) # 5 minutes

# How long an image must be unused before being removed.
IMAGE_IDLE_TIMEOUT = int(os.environ.get('IMAGE_IDLE_TIMEOUT', 1800)) # 30 minutes

# --- Static Configuration ---
PROXY_SERVER_PORT = 80 # The port this proxy server itself listens on.
CONTAINER_STARTUP_TIMEOUT = 30
DOCKER_NETWORK = "dynamic_proxy_net"

# --- Globals ---
app = Flask(__name__)
client = docker.from_env()
container_lock = Lock()
# Tracks the last time an image was requested to manage both container and image lifecycles.
resource_last_access = {}

# --- Helper Functions ---

def ensure_network_exists():
    """Checks if the Docker network exists, and creates it if not."""
    try:
        client.networks.get(DOCKER_NETWORK)
        print(f"Network '{DOCKER_NETWORK}' already exists.")
    except docker.errors.NotFound:
        print(f"Creating network '{DOCKER_NETWORK}'...")
        client.networks.create(DOCKER_NETWORK, driver="bridge")

def connect_self_to_network():
    """Connects the proxy container itself to the target Docker network."""
    try:
        container_id = socket.gethostname()
        proxy_container = client.containers.get(container_id)
        proxy_container.reload()
        if DOCKER_NETWORK in proxy_container.attrs['NetworkSettings']['Networks']:
            print(f"Proxy container is already connected to '{DOCKER_NETWORK}'.")
            return
        print(f"Connecting proxy container ({container_id}) to network '{DOCKER_NETWORK}'...")
        network = client.networks.get(DOCKER_NETWORK)
        network.connect(proxy_container)
        print("Proxy container connected successfully.")
    except docker.errors.NotFound:
        print(f"Could not find self (container {container_id}). This is expected if running outside of Docker.")
    except Exception as e:
        print(f"An error occurred while connecting self to network: {e}")

def get_container_name(image_tag):
    """Generates a consistent and safe container name from a full image tag."""
    sanitized_tag = image_tag.replace('/', '--').replace(':', '-')
    return f"proxy-child-{sanitized_tag}"

def cleanup_idle_resources():
    """A background thread that stops idle containers and removes unused images."""
    print("Cleanup thread started.")
    while True:
        time.sleep(60)  # Run cleanup every minute
        with container_lock:
            now = time.time()

            # --- Part 1: Stop Idle Containers ---
            try:
                proxy_containers = client.containers.list(filters={"label": "dev.gemini.proxy.image-name"})
                for container in proxy_containers:
                    image_name = container.labels.get("dev.gemini.proxy.image-name")
                    if not image_name:
                        continue
                    
                    last_used = resource_last_access.get(image_name)
                    if last_used and (now - last_used > CONTAINER_IDLE_TIMEOUT):
                        if container.status == 'running':
                            print(f"Container '{container.name}' for image '{image_name}' is idle. Stopping...")
                            container.stop()
            except Exception as e:
                print(f"Error during container cleanup scan: {e}")

            # --- Part 2: Remove Idle Images ---
            time.sleep(2) # Give a moment for containers to enter 'exited' state

            # Get all images that still have running proxy-child containers
            active_images = set()
            running_proxy_containers = client.containers.list(filters={"label": "dev.gemini.proxy.image-name", "status": "running"})
            for container in running_proxy_containers:
                 image_name = container.labels.get("dev.gemini.proxy.image-name")
                 if image_name:
                    active_images.add(image_name)

            # Check which images are now fully idle (no running containers)
            images_to_remove = []
            for image_name, last_used in resource_last_access.items():
                if image_name not in active_images:
                    if (now - last_used > IMAGE_IDLE_TIMEOUT):
                        images_to_remove.append(image_name)

            for image_name in images_to_remove:
                try:
                    print(f"Image '{image_name}' is idle and unused. Removing...")
                    client.images.remove(image=image_name, force=False)
                    del resource_last_access[image_name]
                    print(f"Successfully removed image '{image_name}'.")
                except docker.errors.ImageNotFound:
                    del resource_last_access[image_name]
                except docker.errors.APIError as e:
                    print(f"Could not remove image '{image_name}': {e.strerror}. It may be in use by another tag or container.")
                except Exception as e:
                    print(f"An unexpected error occurred while removing image {image_name}: {e}")


# --- Main Proxy Logic ---

def resolve_image_and_path(path):
    """Determines the full Docker image name and remaining path based on configuration."""
    if PROXY_BASE_IMAGE:
        path_parts = path.strip('/').split('/')
        tag = path_parts[0]
        image_name = f"{PROXY_BASE_IMAGE}:{tag}"
        remaining_path = "/".join(path_parts[1:])
        return image_name, remaining_path
    else:
        path_parts = path.strip('/').split('/')
        for i in range(len(path_parts), 0, -1):
            image_candidate = "/".join(path_parts[:i])
            try:
                client.images.get(image_candidate)
                return image_candidate, "/".join(path_parts[i:])
            except docker.errors.ImageNotFound:
                try:
                    print(f"Pulling image '{image_candidate}'...")
                    client.images.pull(image_candidate)
                    return image_candidate, "/".join(path_parts[i:])
                except docker.errors.ImageNotFound:
                    continue
        return None, None

route_path = f"/{PROXY_BASE_PATH}/<path:path>" if PROXY_BASE_PATH else "/<path:path>"

@app.route(route_path, methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
def proxy(path):
    """The main proxy route. It now uses configuration to resolve images and paths."""
    image_name_found, remaining_path = resolve_image_and_path(path)

    if not image_name_found:
        return f"Service not found. Could not resolve a valid Docker image for path '{path}'", 404

    try:
        client.images.get(image_name_found)
    except docker.errors.ImageNotFound:
        try:
            print(f"Pulling final image '{image_name_found}'...")
            client.images.pull(image_name_found)
        except docker.errors.ImageNotFound:
            return f"Image '{image_name_found}' could not be found.", 404
        except Exception as e:
            return f"Error pulling image '{image_name_found}': {e}", 500

    container_name = get_container_name(image_name_found)
    service_ready = False

    with container_lock:
        try:
            client.containers.get(container_name)
            service_ready = True
            print(f"Container '{container_name}' is already running.")
        except docker.errors.NotFound:
            print(f"Container '{container_name}' not found. Starting for image '{image_name_found}'...")
            target_container = None
            try:
                target_container = client.containers.run(
                    image_name_found,
                    detach=True,
                    name=container_name,
                    network=DOCKER_NETWORK,
                    remove=True,
                    labels={"dev.gemini.proxy.image-name": image_name_found}
                )
                print(f"Started container '{container_name}' ({target_container.short_id})")
                start_time = time.time()
                print(f"Waiting for service on port {PROXY_TARGET_PORT} in NEW container '{container_name}'...")
                health_check_url = f"http://{container_name}:{PROXY_TARGET_PORT}/"
                while time.time() - start_time < CONTAINER_STARTUP_TIMEOUT:
                    try:
                        requests.get(health_check_url, timeout=1, headers={'User-Agent': 'Docker-Proxy-Health-Check/1.0'})
                        service_ready = True
                        print(f"Service '{container_name}' is ready.")
                        break
                    except requests.exceptions.RequestException:
                        time.sleep(0.5)
                if not service_ready:
                    print(f"ERROR: Service in container '{container_name}' failed to become healthy.")
                    target_container.stop()
            except Exception as e:
                print(f"ERROR: Could not start container for image '{image_name_found}': {e}")
                if target_container: target_container.stop()
                return "Error starting service.", 500

    if not service_ready:
        return "Service failed to start in time.", 504

    resource_last_access[image_name_found] = time.time()

    try:
        target_url = f"http://{container_name}:{PROXY_TARGET_PORT}/{remaining_path}"
        print(f"Proxying request for '{path}' to {target_url}")
        resp = requests.request(
            method=request.method,
            url=target_url,
            headers={key: value for (key, value) in request.headers if key.lower() != 'host'},
            data=request.get_data(),
            cookies=request.cookies,
            allow_redirects=False,
            stream=True,
            timeout=(5, 30)
        )
        return Response(resp.iter_content(chunk_size=1024), status=resp.status_code,
                        headers=resp.headers.items(), content_type=resp.headers.get('Content-Type'))
    except requests.exceptions.RequestException as e:
        print(f"ERROR: Failed to proxy request to '{container_name}': {e}")
        return "Error communicating with the service.", 502

if __name__ == '__main__':
    print("--- Dynamic Proxy Server (v7) ---")
    print(f"Mode: {'Base Image' if PROXY_BASE_IMAGE else 'Full Path Resolution'}")
    if PROXY_BASE_IMAGE:
        print(f"-> Base Image: {PROXY_BASE_IMAGE}")
        print(f"-> URL Structure: /{(PROXY_BASE_PATH + '/') if PROXY_BASE_PATH else ''}<tag>/<...>")
    print(f"-> Target Port: {PROXY_TARGET_PORT}")
    print(f"-> Container Idle Timeout: {CONTAINER_IDLE_TIMEOUT}s")
    print(f"-> Image Idle Timeout: {IMAGE_IDLE_TIMEOUT}s")
    print("---------------------------------")
    
    ensure_network_exists()
    connect_self_to_network()
    idle_monitor = Thread(target=cleanup_idle_resources, daemon=True)
    idle_monitor.start()
    app.run(host='0.0.0.0', port=PROXY_SERVER_PORT)
import docker
import os
import time
import socket
from flask import Flask, request, Response
import requests
from threading import Thread, Lock

# --- Configuration from Environment Variables ---
# The base URL path for the proxy. e.g., "preview" -> /preview/<tag>
BASE_PATH = os.environ.get('BASE_PATH', '').strip('/')

# The base Docker image. e.g., "my-org/my-app" -> my-org/my-app:<tag>
IMAGE = os.environ.get('IMAGE', '')

# Validate required configuration
if not BASE_PATH:
    print("ERROR: BASE_PATH environment variable is required.")
    print("Please set BASE_PATH to specify the URL prefix for proxy routes.")
    print("Example: BASE_PATH=preview")
    exit(1)

if not IMAGE:
    print("ERROR: IMAGE environment variable is required.")
    print("Please set IMAGE to specify the base Docker image name.")
    print("Example: IMAGE=my-org/my-app")
    exit(1)

# The port the target containers are expected to listen on.
PORT = int(os.environ.get('PORT', 80))

# How long a container can be idle before being stopped.
CONTAINER_TIMEOUT = int(os.environ.get('CONTAINER_TIMEOUT', 300)) # 5 minutes

# How long an image must be unused before being removed.
IMAGE_TIMEOUT = int(os.environ.get('IMAGE_TIMEOUT', 1800)) # 30 minutes

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

def is_local_image(image_name):
    """Determines if an image is local (not from a remote registry)."""
    try:
        image_obj = client.images.get(image_name)
        
        # If the image has no RepoDigests, it's likely local (not pulled from registry)
        if not image_obj.attrs.get('RepoDigests'):
            return True
            
        # Check if any of the RepoDigests point to known registries
        repo_digests = image_obj.attrs.get('RepoDigests', [])
        for digest in repo_digests:
            # If digest contains a known registry, it's remote
            if any(registry in digest for registry in ['docker.io', 'ghcr.io', 'quay.io', 'gcr.io', 'ecr.']):
                return False
        
        # If we get here, it's likely local
        return True
        
    except docker.errors.ImageNotFound:
        # If we can't find the image, assume it's local to be safe
        return True
    except Exception as e:
        print(f"Error checking if image '{image_name}' is local: {e}")
        # Default to local to be safe
        return True

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
                    if last_used and (now - last_used > CONTAINER_TIMEOUT):
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
            images_to_untrack = []
            for image_name, last_used in list(resource_last_access.items()):
                if image_name not in active_images:
                    if (now - last_used > IMAGE_TIMEOUT):
                        if is_local_image(image_name):
                            print(f"Image '{image_name}' is local and will not be removed.")
                            # Mark for removal from tracking since we won't clean it up
                            images_to_untrack.append(image_name)
                        else:
                            images_to_remove.append(image_name)
            
            # Remove local images from tracking
            for image_name in images_to_untrack:
                del resource_last_access[image_name]

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
    path_parts = path.strip('/').split('/')
    if not path_parts or not path_parts[0]:
        return None, None
    
    tag = path_parts[0]
    image_name = f"{IMAGE}:{tag}"
    remaining_path = "/".join(path_parts[1:])
    return image_name, remaining_path

route_path = f"/{BASE_PATH}/<path:path>"

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
                print(f"Waiting for service on port {PORT} in NEW container '{container_name}'...")
                health_check_url = f"http://{container_name}:{PORT}/"
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
        target_url = f"http://{container_name}:{PORT}/{remaining_path}"
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
    print("--- Dynamic Proxy Server (v8) ---")
    print(f"-> Base Path: /{BASE_PATH}")
    print(f"-> Base Image: {IMAGE}")
    print(f"-> URL Structure: /{BASE_PATH}/<tag>/<...>")
    print(f"-> Target Port: {PORT}")
    print(f"-> Container Timeout: {CONTAINER_TIMEOUT}s")
    print(f"-> Image Timeout: {IMAGE_TIMEOUT}s")
    print("---------------------------------")
    
    ensure_network_exists()
    connect_self_to_network()
    idle_monitor = Thread(target=cleanup_idle_resources, daemon=True)
    idle_monitor.start()
    app.run(host='0.0.0.0', port=PROXY_SERVER_PORT)
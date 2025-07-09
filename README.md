# Preview Proxy

This is a dynamic proxy server that launches other Docker containers on-demand based on the URL path of incoming requests. It's designed to dynamically route traffic to different backend services, each running in its own container, without needing a predefined configuration for each one.

The public Docker image is available at `ghcr.io/phenome/preview-proxy`.

## How It Works

When the proxy server receives a request, it interprets the URL path to determine which Docker image to use.

1.  **Image Resolution**: The proxy constructs a Docker image name from the request path. For example, a request to `/my-service/some/path` could resolve to the Docker image `my-service`.
2.  **Container Launch**: If a container for that image isn't already running, the proxy will:
    *   Pull the required image from a Docker registry if it's not available locally.
    *   Start a new container from that image.
    *   Wait for the service inside the container to become healthy.
3.  **Request Proxying**: Once the target container is running and healthy, the proxy forwards the request to it.
4.  **Idle Cleanup**: The proxy monitors running containers and idle images. If a container or image is unused for a configurable period, it will be stopped and/or removed to save resources.

This allows you to have a single entry point that can serve multiple, independent applications, which are only started when they are actually needed.

## Usage

To use the proxy, you need to have Docker installed. You can run the proxy using the `docker run` command.

```bash
docker run -d \
  --name preview-proxy \
  -p 8080:80 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  ghcr.io/phenome/preview-proxy
```

**Explanation:**

*   `-d`: Run the container in detached mode.
*   `--name preview-proxy`: Give the container a name.
*   `-p 8080:80`: Map port 8080 on your host to port 80 inside the proxy container. You can change `8080` to any port you prefer.
*   `-v /var/run/docker.sock:/var/run/docker.sock`: **CRITICAL**. This mounts the Docker socket from the host into the container, allowing the proxy to start and manage other containers.
*   `ghcr.io/phenome/preview-proxy`: The public image to run.

## Configuration

The proxy's behavior can be customized using the following environment variables:

| Variable | Description | Default |
| :--- | :--- | :--- |
| `PROXY_BASE_PATH` | A URL prefix for all proxy routes. If set to `preview`, requests will be expected at `/preview/...`. | `''` (empty) |
| `PROXY_BASE_IMAGE` | A base image name to prepend to the tag found in the URL. If set to `my-org/my-app`, a request to `/v1/` will resolve to the image `my-org/my-app:v1`. | `''` (empty) |
| `PROXY_TARGET_PORT` | The internal port that the spawned containers are expected to be listening on. | `80` |
| `CONTAINER_IDLE_TIMEOUT` | The number of seconds a container can be idle (no requests) before it is automatically stopped. | `300` (5 minutes) |
| `IMAGE_IDLE_TIMEOUT` | The number of seconds an image can be unused (no running containers) before it is removed from the host. | `1800` (30 minutes) |

### Example: Running with custom configuration

This command starts the proxy to handle requests under the `/services/` path and expects target containers to listen on port 3000.

```bash
docker run -d \
  --name preview-proxy \
  -p 8080:80 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e PROXY_BASE_PATH=services \
  -e PROXY_TARGET_PORT=3000 \
  -e CONTAINER_IDLE_TIMEOUT=600 \
  ghcr.io/phenome/preview-proxy
```

With this configuration, a request to `http://localhost:8080/services/nginx/` would cause the proxy to:
1.  Look for the `nginx` Docker image.
2.  Start a container from it.
3.  Proxy the request to port 3000 inside that new container.

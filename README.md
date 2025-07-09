# Preview Proxy

This is a dynamic proxy server that launches other Docker containers on-demand based on the URL path of incoming requests. It's designed to dynamically route traffic to different backend services, each running in its own container, without needing a predefined configuration for each one.

The public Docker image is available at `ghcr.io/phenome/preview-proxy`.

## How It Works

When the proxy server receives a request, it interprets the URL path to determine which Docker image to use.

1.  **Image Resolution**: The proxy constructs a Docker image name from the request path using the configured base image. For example, with `IMAGE=my-org/my-app`, a request to `/preview/v1/some/path` would resolve to the Docker image `my-org/my-app:v1`.
2.  **Container Launch**: If a container for that image isn't already running, the proxy will:
    *   Pull the required image from a Docker registry if it's not available locally.
    *   Start a new container from that image.
    *   Wait for the service inside the container to become healthy.
3.  **Request Proxying**: Once the target container is running and healthy, the proxy forwards the request to it.
4.  **Idle Cleanup**: The proxy monitors running containers and idle images. If a container is unused for a configurable period, it will be stopped. Remote images (from registries) that are unused will be removed to save resources, but local images are preserved since they cannot be pulled again.

This allows you to have a single entry point that can serve multiple, independent applications, which are only started when they are actually needed.

## Usage

To use the proxy, you need to have Docker installed. You can run the proxy using the `docker run` command.

```bash
docker run -d \
  --name preview-proxy \
  -p 8080:80 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e BASE_PATH=preview \
  -e IMAGE=my-org/my-app \
  ghcr.io/phenome/preview-proxy
```

**Explanation:**

*   `-d`: Run the container in detached mode.
*   `--name preview-proxy`: Give the container a name.
*   `-p 8080:80`: Map port 8080 on your host to port 80 inside the proxy container. You can change `8080` to any port you prefer.
*   `-v /var/run/docker.sock:/var/run/docker.sock`: **CRITICAL**. This mounts the Docker socket from the host into the container, allowing the proxy to start and manage other containers.
*   `-e BASE_PATH=preview`: **REQUIRED**. Sets the URL prefix for proxy routes.
*   `-e IMAGE=my-org/my-app`: **REQUIRED**. Sets the base Docker image name.
*   `ghcr.io/phenome/preview-proxy`: The public image to run.

## Configuration

The proxy's behavior can be customized using the following environment variables:

| Variable | Description | Required | Default |
| :--- | :--- | :--- | :--- |
| `BASE_PATH` | A URL prefix for all proxy routes. If set to `preview`, requests will be expected at `/preview/...`. | **Yes** | - |
| `IMAGE` | A base image name to prepend to the tag found in the URL. If set to `my-org/my-app`, a request to `/v1/` will resolve to the image `my-org/my-app:v1`. | **Yes** | - |
| `PORT` | The internal port that the spawned containers are expected to be listening on. | No | `80` |
| `CONTAINER_TIMEOUT` | The number of seconds a container can be idle (no requests) before it is automatically stopped. | No | `300` (5 minutes) |
| `IMAGE_TIMEOUT` | The number of seconds a remote image can be unused (no running containers) before it is removed from the host. Local images are never removed. | No | `1800` (30 minutes) |

### Example: Running with custom configuration

This command starts the proxy to handle requests under the `/services/` path and expects target containers to listen on port 3000.

```bash
docker run -d \
  --name preview-proxy \
  -p 8080:80 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e BASE_PATH=services \
  -e IMAGE=my-org/my-app \
  -e PORT=3000 \
  -e CONTAINER_TIMEOUT=600 \
  ghcr.io/phenome/preview-proxy
```

With this configuration, a request to `http://localhost:8080/services/v1/` would cause the proxy to:
1.  Look for the `my-org/my-app:v1` Docker image.
2.  Start a container from it.
3.  Proxy the request to port 3000 inside that new container.

# KubeSage in a container: the FinOps pipeline + Control Center, for running in-cluster
# (see deploy/k8s/kubesage/ and docs/in-cluster.md) or anywhere Docker runs.
# Build:  docker build -t ghcr.io/YOU/kubesage:latest .
# Run UI: docker run -p 8600:8600 -e MODEL_API_KEY=... ghcr.io/YOU/kubesage:latest
FROM python:3.12-slim

ARG KUBECTL_VERSION=v1.36.4
ARG TARGETARCH

# git + ssh: the GitOps flow clones, commits and pushes from inside the container.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssh-client ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL -o /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH:-amd64}/kubectl" \
 && chmod +x /usr/local/bin/kubectl

WORKDIR /app
COPY . .
# Editable install on purpose: kubesage/web/server.py resolves dashboard/, config/ and
# agents/ relative to the source tree, so the package must stay at /app/kubesage.
RUN pip install --no-cache-dir -e . \
 && useradd --uid 10001 --create-home kubesage \
 && mkdir -p /app/out && chown -R kubesage:kubesage /app

USER kubesage
ENV PYTHONUNBUFFERED=1 KUBESAGE_CONFIG_DIR=/app/config
EXPOSE 8600
ENTRYPOINT ["kubesage"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8600"]

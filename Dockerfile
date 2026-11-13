# Start with a small Linux image that already has Python 3.12 installed.
# Pinned by digest, so a changed upstream tag cannot change what we build. Update the digest on purpose.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

# Build-time settings for choosing the kubectl version and CPU architecture.
# Keep KUBECTL_VERSION within one minor version of the node image in kind.yaml.
ARG KUBECTL_VERSION=1.31.4
ARG TARGETARCH

# Install kubectl, check it against the official checksum, and then remove
# build-only tools and package caches to keep the final image smaller.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && arch="${TARGETARCH:-$(dpkg --print-architecture)}" \
    && case "$arch" in aarch64|arm64) arch=arm64 ;; *) arch=amd64 ;; esac \
    && url="https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/${arch}/kubectl" \
    && curl -fsSL -o /usr/local/bin/kubectl "$url" \
    && echo "$(curl -fsSL "$url.sha256")  /usr/local/bin/kubectl" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/kubectl \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Commands below this point run from /app inside the container.
WORKDIR /app

# Copy the files needed to install and run the Python application.
COPY pyproject.toml README.md constraints.txt ./
COPY src ./src
COPY scenarios ./scenarios

# Install this project, with dependencies pinned to the versions the tests passed with.
RUN pip install --no-cache-dir -c constraints.txt .

# Configure the application at runtime.
ENV AGENT_MODEL=live \
    K8S_TROUBLESHOOTER_ROOT=/app \
    TRACE_DIR=/tmp/traces \
    HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1

# Run as an unprivileged user, not root.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin agent
USER 10001

# Run the k8s-troubleshooter command whenever the container starts.
ENTRYPOINT ["k8s-troubleshooter"]

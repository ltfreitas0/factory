# Sandbox container image for the sandbox-gw Worker.
# The @cloudflare/sandbox SDK builds and runs this image for every sandbox.
# Stable line (matches the stable @cloudflare/sandbox npm package).
FROM docker.io/cloudflare/sandbox:0.7.0

# Base image is Ubuntu 22.04 (glibc) with Node 24 + Bun preinstalled.
# This image pins the runtime the factory actually targets: Node 20 (LTS).

# Node.js 20 (LTS) from NodeSource for Ubuntu 22.04, plus git.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates git \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && node --version

# Make Node 20 the default (the base image keeps Node 24 at /usr/local/bin).
RUN ln -sf /usr/bin/node /usr/local/bin/node && node --version

# Bun via the official install script (base image already ships one; this
# pins/refreshes the same stable line into the image).
RUN curl -fsSL https://bun.sh/install | bash \
    && ln -sf /root/.bun/bin/bun /usr/local/bin/bun \
    && bun --version

# Control plane + app ports used by the sandbox runtime.
EXPOSE 8080 3000

FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive

# Install all build dependencies (csh + satdeploy-apm + satdeploy-agent)
RUN apt-get update && apt-get install -y \
    build-essential \
    pkg-config \
    git \
    can-utils \
    fonts-powerline \
    libcurl4-openssl-dev \
    libzmq3-dev \
    libsqlite3-dev \
    libsocketcan-dev \
    libcap2-bin \
    python3-dev \
    python3-venv \
    python3-pip \
    pipx \
    libyaml-dev \
    libelf-dev \
    libbsd-dev \
    libprotobuf-c-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Install meson and ninja via pipx
RUN pipx install meson==1.8.1 ninja
ENV PATH="/root/.local/bin:$PATH"

# Create lib dir for APMs
RUN mkdir -p /root/.local/lib/csh

# Clone and build CSH (public repo, baked into image)
RUN git clone --recurse-submodules https://github.com/spaceinventor/csh.git /csh \
    && cd /csh \
    && meson setup . builddir -Dprefix=/root/.local -Dlibdir=lib/csh \
    && ninja -C builddir \
    && meson install -C builddir --skip-subprojects

# satdeploy-apm and satdeploy-agent are built at container start
# via docker-setup.sh (private repo, mounted in)

WORKDIR /root
CMD ["bash"]

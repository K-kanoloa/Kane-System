# Development/VM validation tools only. Never an Agent or system runtime image.
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac
RUN --mount=type=secret,id=proxy_ca,required=true \
    apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca update \
    && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca install -y --no-install-recommends \
       qemu-system-x86 qemu-utils genisoimage systemd util-linux passwd ca-certificates \
    && rm -rf /var/lib/apt/lists/*

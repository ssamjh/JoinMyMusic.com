FROM debian:bookworm-slim

ARG GO_LIBRESPOT_VERSION=v0.10.2
ARG GO_LIBRESPOT_SHA256=9d2b8b2c85606f5c8e75f9f48910d711cab7406f3582091126b3f3063ee0282e
ARG VBAN_VERSION=v2.2.0
ARG VBAN_EMITTER_SHA256=48f8ef69f4d0521450d1937116f5fb4f0e9f672964b34ac1d01bc00329998dd0
ARG VBAN_RECEPTOR_SHA256=efb0ad75bac5e50cb96482dbc41971ce2c361edb6c81acdf5dec34bb7cbf44ce

# VBAN's upstream release binaries are Linux amd64 only.
RUN if [ "$(dpkg --print-architecture)" != amd64 ]; then \
        echo "This image requires Linux amd64 for the prebuilt VBAN emitter." >&2; exit 1; fi \
    && apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates python3 python3-yaml python3-zmq ffmpeg liquidsoap=2.1.3-2 \
        libasound2 libpulse0 libjack-jackd2-0 libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

ADD https://github.com/devgianlu/go-librespot/releases/download/${GO_LIBRESPOT_VERSION}/go-librespot_linux_x86_64.tar.gz /tmp/go-librespot.tar.gz
ADD https://github.com/quiniouben/vban/releases/download/${VBAN_VERSION}/vban_emitter /usr/bin/vban_emitter
ADD https://github.com/quiniouben/vban/releases/download/${VBAN_VERSION}/vban_receptor /usr/bin/vban_receptor
ADD https://raw.githubusercontent.com/devgianlu/go-librespot/${GO_LIBRESPOT_VERSION}/LICENSE /usr/share/doc/go-librespot/LICENSE
ADD https://raw.githubusercontent.com/quiniouben/vban/${VBAN_VERSION}/COPYING /usr/share/doc/vban/COPYING

RUN echo "${GO_LIBRESPOT_SHA256}  /tmp/go-librespot.tar.gz" | sha256sum -c - \
    && echo "${VBAN_EMITTER_SHA256}  /usr/bin/vban_emitter" | sha256sum -c - \
    && echo "${VBAN_RECEPTOR_SHA256}  /usr/bin/vban_receptor" | sha256sum -c - \
    && tar -xzf /tmp/go-librespot.tar.gz -C /usr/bin go-librespot \
    && rm /tmp/go-librespot.tar.gz \
    && chmod 0755 /usr/bin/go-librespot /usr/bin/vban_emitter /usr/bin/vban_receptor \
    && chmod 0644 /usr/share/doc/go-librespot/LICENSE /usr/share/doc/vban/COPYING \
    && groupadd --system spotify \
    && useradd --system --gid spotify --home-dir /var/lib/go-librespot --no-create-home spotify \
    && mkdir -p /run/audio /var/lib/go-librespot \
    && chown spotify:spotify /run/audio /var/lib/go-librespot

COPY scripts/ /app/scripts/
COPY config/ /etc/jmm/

USER spotify
VOLUME ["/var/lib/go-librespot"]
EXPOSE 3678 8090
CMD ["python3", "/app/scripts/run.py"]

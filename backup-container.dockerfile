FROM python:3.10 AS download

ARG RESTIC_VERSION=0.14.0
ARG RESTIC_DL_HASH=c8da7350dc334cd5eaf13b2c9d6e689d51e7377ba1784cc6d65977bd44ee1165
RUN curl -Lo /tmp/restic_${RESTIC_VERSION}_linux_amd64.bz2 https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/restic_${RESTIC_VERSION}_linux_amd64.bz2 && \
    printf "${RESTIC_DL_HASH}  /tmp/restic_${RESTIC_VERSION}_linux_amd64.bz2\\n" | sha256sum -c && \
    bunzip2 < /tmp/restic_${RESTIC_VERSION}_linux_amd64.bz2 > /restic && \
    chmod +x /restic

ARG QCOW2_WRITER_VERSION=0.1.0
ARG QCOW2_WRITER_DL_HASH=7c6ec8277e31498e5e73ca811c2b5feca7dce4d460b0fee695c4ba74ec63ecde
RUN curl -Lo /tmp/streaming-qcow2-writer_linux_amd64.bz2 https://github.com/remram44/streaming-qcow2-writer/releases/download/v${QCOW2_WRITER_VERSION}/streaming-qcow2-writer_${QCOW2_WRITER_VERSION}_linux_amd64.bz2 && \
    printf "${QCOW2_WRITER_DL_HASH}  /tmp/streaming-qcow2-writer_linux_amd64.bz2\\n" | sha256sum -c && \
    bunzip2 < /tmp/streaming-qcow2-writer_linux_amd64.bz2 > /streaming-qcow2-writer && \
    chmod +x /streaming-qcow2-writer

FROM ubuntu:22.04
RUN apt-get update -yy && \
    apt-get install -yy curl ca-certificates ceph-common && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
COPY --from=download /restic /usr/local/bin/restic
COPY --from=download /streaming-qcow2-writer /usr/local/bin/streaming-qcow2-writer

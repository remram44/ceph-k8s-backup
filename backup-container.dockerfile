FROM python:3.10 AS download

ARG RESTIC_VERSION=0.16.4
ARG RESTIC_DL_HASH=3d4d43c169a9e28ea76303b1e8b810f0dcede7478555fdaa8959971ad499e324
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

ENV TINI_VERSION v0.19.0
ADD https://github.com/krallin/tini/releases/download/${TINI_VERSION}/tini /tini
RUN chmod +x /tini

RUN apt-get update -yy && \
    apt-get install -yy curl ca-certificates ceph-common && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
COPY --from=download /restic /usr/local/bin/restic
COPY --from=download /streaming-qcow2-writer /usr/local/bin/streaming-qcow2-writer

ENTRYPOINT ["/tini", "--"]

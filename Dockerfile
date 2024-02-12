FROM --platform=$BUILDPLATFORM python:3.10 AS deps

# Install Poetry
RUN curl -sSL https://install.python-poetry.org | python3 - && /root/.local/bin/poetry config virtualenvs.create false

# Copy Poetry data
RUN mkdir -p /usr/src/app
WORKDIR /usr/src/app
COPY pyproject.toml poetry.lock ./

# Generate requirements list
RUN /root/.local/bin/poetry export -o requirements.txt


FROM python:3.10

ENV TINI_VERSION v0.19.0
ADD https://github.com/krallin/tini/releases/download/${TINI_VERSION}/tini /tini
RUN chmod +x /tini

# Install rbd
RUN apt-get update && \
    apt-get install -yy ceph-common && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install requirements
COPY --from=deps /usr/src/app/requirements.txt /requirements.txt
RUN pip --disable-pip-version-check install --no-cache-dir -r /requirements.txt

# Set up app
RUN mkdir -p /usr/src/app
WORKDIR /usr/src/app
COPY ceph_backup ./ceph_backup
RUN printf -- '#!/bin/sh\npython3 -c "from ceph_backup.backup import main; main()" "$@"' > /usr/local/bin/ceph-backup && \
    printf -- '#!/bin/sh\npython3 -c "from ceph_backup.metrics import main; main()" "$@"' > /usr/local/bin/ceph-backup-metrics && \
    chmod +x /usr/local/bin/ceph-backup /usr/local/bin/ceph-backup-metrics

# Set up user
RUN mkdir -p /usr/src/app/home && \
    useradd -d /usr/src/app/home -s /usr/sbin/nologin -u 998 appuser && \
    chown appuser /usr/src/app/home

ENV PYTHONFAULTHANDLER=1

USER 998
ENTRYPOINT ["/tini", "--", "/bin/bash", "-c", "if [ x\"$OTEL_TRACES_EXPORTER\" != x ]; then exec opentelemetry-instrument \"$@\"; else exec \"$@\"; fi", "--"]
CMD ["ceph-backup"]

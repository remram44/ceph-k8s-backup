#!/bin/sh

set -eu

VERSION=$(git describe | sed 's/^v//')
IMAGE=ghcr.io/remram44/ceph-k8s-backup:$VERSION

docker buildx build --pull \
    . \
    --platform linux/amd64,linux/arm/v7,linux/arm64 \
    --push --tag $IMAGE

echo
echo "    $IMAGE"

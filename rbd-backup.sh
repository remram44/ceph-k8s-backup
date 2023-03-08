#!/bin/sh

if [ "$#" != 2 ]; then
    echo "Usage: rbd-backup.sh POOL IMG" >&2
    exit 1
fi
POOL="$1"
IMG="$2"

rbd snap create $POOL/$IMG@backup
rbd snap protect $POOL/$IMG@backup
rbd clone $POOL/$IMG@backup $POOL/backup-$IMG
BLOCK=$(rbd device map $POOL/backup-$IMG)
mount $BLOCK /mnt
tar -zcf /tmp/ceph-$POOL-$IMG.tar.gz -C /mnt .
umount /mnt
rbd device unmap $BLOCK
rbd rm $POOL/backup-$IMG
rbd snap unprotect $POOL/$IMG@backup
rbd snap rm $POOL/$IMG@backup

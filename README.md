This is an application used to make regular backups of Ceph data in a Kubernetes cluster.

It runs a container that periodically enumerates PersistentVolumeClaims on the cluster and backs them up using Restic.

# What's backed up

It supports RBD filesystems, RBD block devices, and CephFS volumes:

* An RBD filesystem is the fastest storage Ceph can provide. It consists of an RBD image on Ceph that is formatted as ext4 and mounted in a pod. Because it uses an emulated device, it can only be mounted read-write on one machine at a time (`ReadWriteOnce` or `ReadOnlyMany`)
    * This tool backs up RBD filesystems by creating a snapshot, creating a new image from the snapshot (we need to write to it to fix the filesystem if it was under use), mounting the image, and running Restic on the filesystem contents
* An RBD block device is a raw RADOS image that is exposed to container as a block device. It is useful for specific situations like running virtualization software. We don't know what's on the image (there can be multiple partitions, any filesystem, etc) and we want exact recovery of the whole disk.
    * This tool backs up RBD block devices by creating a snapshot, reading the image layout from Ceph, and streaming it from Ceph into Restic in QCOW2 format. This method allows us to skip empty blocks in the source (that we discover from the image layout) by creating a sparse QCOW2 file, rather than reading the full image from Ceph which would include unallocated blocks. Streaming it to Restic allows us to consume very little space during the process.
* CephFS volumes are distributed file shares that are accessed using a file-based API. Their advantage is that they can be mounted on multiple machines at the same time, and Ceph can apply access control to directories.
    * TODO: Figure out plan
    * Just do regular snapshots of the CephFS?

# Where is it backed up

The data backed up with Restic ends up in a single Restic repository. Each PersistentVolumeClaim appears as a different hostname with the format `k8s-<kubernetes-namespace>-nspvc-<pvc name>`.

# Configuration

Global configuration:

* How often to run
* The Restic repository

Annotations on Kubernetes namespaces:

* `cephbackup.hpc.nyu.edu/backup` (true/false) indicates that PVCs in this namespace should not be backed up

Annotations on Kubernetes PersistentVolumeClaims (can be set by users):

* `cephbackup.hpc.nyu.edu/backup` (true/false) indicates that this PVC should not be backed up

Annotations on Kubernetes PersistentVolumes:

* `cephbackup.hpc.nyu.edu/backupa (true/false)` indicates that this PV should not be backed up

In addition, an annotation `cephbackup.hpc.nyu.edu/last-backup` is set on the PV by this system to keep track of the data of the last backup.

A volume is backed up if:

* backup is true on the PV
* or backup is not set on the PV and
    * backup is true on the namespace
        * and backup is true or not set on the PVC
    * or backup is not set on the namespace
        * and backup is true or not set on the PVC

This means that an administrator, who can set annotations on namespaces and PVs, can override the decisions of a user, who can only set annotations on PVCs.

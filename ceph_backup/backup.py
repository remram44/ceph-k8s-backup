import argparse
from datetime import datetime
import kubernetes.client as k8s_client
import kubernetes.config as k8s_config
import logging
import math
import os
import shlex
import subprocess

from .metadata import METADATA_PREFIX, ANNOTATION_LAST_ATTEMPT, NAMESPACE, \
    parse_date, list_volumes_to_backup


logger = logging.getLogger(__name__)


CEPH_SECRET_NAME = os.environ.get('CEPH_SECRET_NAME', 'ceph')
RESTIC_SECRET_NAME = os.environ.get('RESTIC_SECRET_NAME', 'restic')


def render_date(dt):
    s = dt.isoformat()
    assert len(s) >= 19 and s[10] == 'T'
    return dt.isoformat()[:19] + 'Z'


def call(args):
    logger.info("> %s", ' '.join(shlex.quote(a) for a in args))
    retcode = subprocess.call(args, stdout=subprocess.DEVNULL)
    logger.info("-> %d", retcode)
    return retcode


def check_call(args):
    retcode = call(args)
    if retcode != 0:
        raise subprocess.CalledProcessError(retcode, args)


def format_env(**kwargs):
    result = []
    for k, v in kwargs.items():
        if isinstance(v, str):
            result.append(k8s_client.V1EnvVar(name=k, value=v))
        elif isinstance(v, tuple) and v[0] == 'secret':
            assert len(v) == 3
            result.append(k8s_client.V1EnvVar(
                name=k,
                value_from=k8s_client.V1EnvVarSource(
                    secret_key_ref=k8s_client.V1SecretKeySelector(
                        name=v[1],
                        key=v[2],
                    ),
                )),
            )
        else:
            assert False, v
    return result


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    now = datetime.utcnow()

    parser = argparse.ArgumentParser(
        'ceph-backup',
        description="Backup up Ceph volumes on a Kubernetes cluster",
    )
    parser.add_argument('--kubeconfig', nargs=1)
    args = parser.parse_args()

    if args.kubeconfig:
        logger.info("Using specified config file")
        k8s_config.load_kube_config(args.kubeconfig[0])
    else:
        logger.info("Using in-cluster config")
        k8s_config.load_incluster_config()

    ceph = {
        'monitors': [
            mon for mon in os.environ['CEPH_MONITORS'].split(',')
            if mon
        ],
        'secret': CEPH_SECRET_NAME,
        'user': os.environ['CEPH_USER'],
    }

    api = k8s_client.ApiClient()
    corev1 = k8s_client.CoreV1Api(api)

    # Clean old jobs
    cleanup_jobs(api)

    # Back up volumes
    to_backup = build_list_to_backup(api, now)
    for vol in to_backup:
        if vol['mode'] == 'Filesystem':
            backup_rbd_fs(api, ceph, vol, now)
        else:
            logger.warning("Unsupported volume mode %r", vol['mode'])

            # Annotate the PV anyway so we can keep going
            corev1.patch_persistent_volume(vol['pv'], {
                'metadata': {
                    'annotations': {
                        ANNOTATION_LAST_ATTEMPT: render_date(now),
                    },
                },
            })


def build_list_to_backup(api, now):
    to_backup = list_volumes_to_backup(api)

    # Select based on last attempt
    limit = 24 * 3600 - 30 * 60  # 23:30:00
    to_backup = [
        vol for vol in to_backup
        if (
            vol['last_backup'] is None
            or (now - vol['last_backup']).total_seconds() > limit
        )
    ]

    # Order the list by last backup
    time_zero = datetime(1970, 1, 1)
    to_backup = sorted(
        to_backup,
        key=lambda vol: vol['last_backup'] or time_zero,
    )

    # Instead of doing all the backups that are due right now,
    # we do 1/24th of the backups that are due
    # This is to spread out the backup times if they all coincide
    do_now = math.ceil(len(to_backup) / 24)
    logger.info("%d volumes to backup, doing %d now", len(to_backup), do_now)
    to_backup = to_backup[:do_now]

    return to_backup


def cleanup_jobs(api):
    corev1 = k8s_client.CoreV1Api(api)
    batchv1 = k8s_client.BatchV1Api(api)
    jobs = batchv1.list_namespaced_job(
        NAMESPACE,
        label_selector=METADATA_PREFIX + 'volume-type=rbd',
    ).items
    for job in jobs:
        if not job.status.completion_time:
            continue

        meta = job.metadata
        pvc_namespace = meta.labels[METADATA_PREFIX + 'pvc-namespace']
        pvc_name = meta.labels[METADATA_PREFIX + 'pvc-name']
        pv = meta.labels[METADATA_PREFIX + 'pv-name']

        logger.info(
            "Cleaning up job=%s pv=%s, pvc=%s/%s",
            meta.name,
            pv,
            pvc_namespace,
            pvc_name,
        )

        # Get start time
        start_time = meta.annotations[METADATA_PREFIX + 'start-time']

        # Annotate the PVC
        try:
            pvc = corev1.read_namespaced_persistent_volume_claim(
                pvc_name,
                pvc_namespace,
            )
        except k8s_client.ApiException as e:
            if e.status != 404:
                raise
        else:
            # Don't update if the PVC has a more recent time already
            existing_time = pvc.metadata.annotations.get(
                METADATA_PREFIX + 'last-backup'
            )
            if (
                not existing_time
                or parse_date(existing_time) < parse_date(start_time)
            ):
                corev1.patch_namespaced_persistent_volume_claim(
                    pvc_name,
                    pvc_namespace,
                    {
                        'metadata': {
                            METADATA_PREFIX + 'last-backup': start_time,
                        },
                    },
                )

        # Clean the snapshot and cloned image
        rbd_pool = meta.labels[METADATA_PREFIX + 'rbd-pool']
        rbd_name = meta.labels[METADATA_PREFIX + 'rbd-name']
        rbd_fq_backup_img = rbd_pool + '/' + 'backup-' + rbd_name
        rbd_fq_snapshot = rbd_pool + '/' + rbd_name + '@backup'
        if call(['rbd', 'info', rbd_fq_backup_img]) == 0:
            check_call(['rbd', 'rm', rbd_fq_backup_img])
        if call(['rbd', 'info', rbd_fq_snapshot]) == 0:
            call(['rbd', 'snap', 'unprotect', rbd_fq_snapshot])
            check_call(['rbd', 'snap', 'rm', rbd_fq_snapshot])


def backup_rbd_fs(api, ceph, vol, now):
    logger.info(
        'Backing up: pv=%s, pvc=%s/%s, rbd=%s/%s, mode=%s, size=%s',
        vol['pv'],
        vol['namespace'], vol['name'],
        vol['rbd_pool'], vol['rbd_name'],
        vol['mode'],
        vol['size'] or 'unknown',
    )

    corev1 = k8s_client.CoreV1Api(api)
    batchv1 = k8s_client.BatchV1Api(api)

    # Annotate the PV
    corev1.patch_persistent_volume(vol['pv'], {
        'metadata': {
            'annotations': {
                ANNOTATION_LAST_ATTEMPT: render_date(now),
            },
        },
    })

    rbd_fq_image = vol['rbd_pool'] + '/' + vol['rbd_name']
    rbd_fq_snapshot = rbd_fq_image + '@backup'
    rbd_backup_img = 'backup-' + vol['rbd_name']
    rbd_fq_backup_img = vol['rbd_pool'] + '/' + rbd_backup_img

    # Clean old snapshots and cloned images for this image
    if call(['rbd', 'info', rbd_fq_backup_img]) == 0:
        check_call(['rbd', 'rm', rbd_fq_backup_img])
    if call(['rbd', 'info', rbd_fq_snapshot]) == 0:
        call(['rbd', 'snap', 'unprotect', rbd_fq_snapshot])
        check_call(['rbd', 'snap', 'rm', rbd_fq_snapshot])

    # Make a snapshot
    check_call(['rbd', 'snap', 'create', rbd_fq_snapshot])

    # Turn it into an image, so the filesystem can be fixed on mount
    # (if the image was in use when snapshotting, it will need repair)
    check_call(['rbd', 'snap', 'protect', rbd_fq_snapshot])
    check_call(['rbd', 'clone', rbd_fq_snapshot, rbd_fq_backup_img])

    # Create a job to do the backup
    labels = {
        METADATA_PREFIX + 'volume-type': 'rbd',
        METADATA_PREFIX + 'volume-mode': 'filesystem',
        METADATA_PREFIX + 'pv-name': vol['pv'],
        METADATA_PREFIX + 'pvc-namespace': vol['namespace'],
        METADATA_PREFIX + 'pvc-name': vol['name'],
        METADATA_PREFIX + 'rbd-pool': vol['rbd_pool'],
        METADATA_PREFIX + 'rbd-name': vol['rbd_name'],
    }
    job = batchv1.create_namespaced_job(NAMESPACE, k8s_client.V1Job(
        metadata=k8s_client.V1ObjectMeta(
            generate_name='backup-rbd-fs-%s-' % vol['namespace'],
            labels=labels,
            annotations={
                METADATA_PREFIX + 'start-time': render_date(now),
            },
        ),
        spec=k8s_client.V1JobSpec(
            active_deadline_seconds=12 * 3600,
            ttl_seconds_after_finished=23 * 3600,
            template=k8s_client.V1PodTemplateSpec(
                metadata=k8s_client.V1ObjectMeta(
                    labels=labels,
                ),
                spec=k8s_client.V1PodSpec(
                    restart_policy='Never',
                    containers=[
                        k8s_client.V1Container(
                            name='backup',
                            image='quay.io/remram44/restic',
                            args=[
                                '/opt/restic',
                                '-r', '$(URL)',
                                '--host', '$(HOST)',
                                '--exclude', 'lost+found',
                                'backup', '/data',
                            ],
                            env=format_env(
                                URL=('secret', RESTIC_SECRET_NAME, 'url'),
                                HOST='rbd-fs-%s-nspvc-%s' % (
                                    vol['namespace'],
                                    vol['name'],
                                ),
                                RESTIC_PASSWORD=(
                                    'secret', RESTIC_SECRET_NAME, 'password',
                                ),
                            ),
                            volume_mounts=[
                                k8s_client.V1VolumeMount(
                                    mount_path='/data',
                                    name='data',
                                    read_only=True,
                                ),
                            ],
                        ),
                    ],
                    volumes=[
                        k8s_client.V1Volume(
                            name='data',
                            rbd=k8s_client.V1RBDVolumeSource(
                                monitors=ceph['monitors'],
                                pool=vol['rbd_pool'],
                                image=rbd_backup_img,
                                fs_type=vol['csi']['fstype'],
                                secret_ref=k8s_client.V1SecretReference(
                                    name=ceph['secret'],
                                ),
                                user=ceph['user'],
                            ),
                        ),
                    ],
                ),
            ),
        ),
    ))
    logger.info("Created job %s", job.metadata.name)

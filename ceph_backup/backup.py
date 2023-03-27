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


CEPH_KEY_SECRET_NAME = os.environ.get('CEPH_KEY_SECRET_NAME', 'ceph-key')
RESTIC_SECRET_NAME = os.environ.get('RESTIC_SECRET_NAME', 'restic')

BACKUP_IMAGE = os.environ.get(
    'BACKUP_IMAGE',
    'registry.hsrn.nyu.edu/hsrn-projects/ceph-backup/restic-container:'
    + '0.1.0-18-ge6487ef',
)


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


def check_output(args):
    logger.info("> %s", ' '.join(shlex.quote(a) for a in args))
    with subprocess.Popen(args, stdout=subprocess.PIPE) as process:
        stdout, stderr = process.communicate()
        retcode = process.poll()
    logger.info("-> %d", retcode)
    if retcode != 0:
        raise subprocess.CalledProcessError(retcode, args)
    return stdout


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
        'secret': CEPH_KEY_SECRET_NAME,
        'user': os.environ['CEPH_USER'],
    }

    api = k8s_client.ApiClient()
    corev1 = k8s_client.CoreV1Api(api)

    # Clean old jobs
    currently_backing_up = cleanup_jobs(api)

    # Back up volumes
    to_backup = build_list_to_backup(api, now)
    for vol in to_backup:
        if vol['pv'] in currently_backing_up:
            logger.warning(
                "Skipping backup, job exists: pv=%s, pvc=%s/%s, rbd=%s/%s, "
                + "mode=%s, size=%s, job=%s",
                vol['pv'],
                vol['namespace'], vol['name'],
                vol['rbd_pool'], vol['rbd_name'],
                vol['mode'],
                vol['size'] or 'unknown',
                currently_backing_up[vol['pv']],
            )
            continue

        logger.info(
            'Backing up: pv=%s, pvc=%s/%s, rbd=%s/%s, mode=%s, size=%s',
            vol['pv'],
            vol['namespace'], vol['name'],
            vol['rbd_pool'], vol['rbd_name'],
            vol['mode'],
            vol['size'] or 'unknown',
        )

        # Annotate the PV
        corev1.patch_persistent_volume(vol['pv'], {
            'metadata': {
                'annotations': {
                    ANNOTATION_LAST_ATTEMPT: render_date(now),
                },
            },
        })

        if vol['mode'] == 'Filesystem':
            backup_rbd_fs(api, ceph, vol, now)
        else:
            backup_rbd_block(api, ceph, vol, now)


def build_list_to_backup(api, now):
    to_backup = list_volumes_to_backup(api)

    total_volumes = len(to_backup)

    # Select based on last attempt
    limit = 24 * 3600 - 30 * 60  # 23:30:00
    to_backup = [
        vol for vol in to_backup
        if (
            vol['last_attempt'] is None
            or (now - vol['last_attempt']).total_seconds() > limit
        )
    ]

    # Order the list by last backup
    time_zero = datetime(1970, 1, 1)
    to_backup = sorted(
        to_backup,
        key=lambda vol: vol['last_attempt'] or time_zero,
    )

    # Instead of doing all the backups that are due right now,
    # we do 1/24th of the total backups
    # This is to spread out the backup times if they all coincide
    do_now = min(math.ceil(total_volumes / 24), len(to_backup))
    logger.info("%d volumes to backup, doing %d now", len(to_backup), do_now)
    to_backup = to_backup[:do_now]

    return to_backup


def cleanup_jobs(api):
    currently_backing_up = {}

    corev1 = k8s_client.CoreV1Api(api)
    batchv1 = k8s_client.BatchV1Api(api)
    jobs = batchv1.list_namespaced_job(
        NAMESPACE,
        label_selector=METADATA_PREFIX + 'volume-type=rbd',
    ).items
    for job in jobs:
        meta = job.metadata
        pvc_namespace = meta.labels[METADATA_PREFIX + 'pvc-namespace']
        pvc_name = meta.labels[METADATA_PREFIX + 'pvc-name']
        pv = meta.labels[METADATA_PREFIX + 'pv-name']

        completed = False
        if job.status.completion_time:
            completed = True
        if any(
            condition.type == 'Failed' and condition.status is True
            for condition in job.status.conditions or ()
        ):
            completed = True

        if not completed:
            # Don't start another backup before this job has finished
            currently_backing_up[pv] = job.metadata.name

            # Don't clean up
            continue

        if job.metadata.annotations.get(METADATA_PREFIX + 'cleaned-up'):
            continue

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
                            'annotations': {
                                METADATA_PREFIX + 'last-backup': start_time,
                            },
                        },
                    },
                )

        # Remove the snapshot and cloned image
        rbd_pool = meta.labels[METADATA_PREFIX + 'rbd-pool']
        rbd_name = meta.labels[METADATA_PREFIX + 'rbd-name']
        rbd_fq_backup_img = rbd_pool + '/' + 'backup-' + rbd_name
        rbd_fq_snapshot = rbd_pool + '/' + rbd_name + '@backup'
        if call(['rbd', 'info', rbd_fq_backup_img]) == 0:
            check_call(['rbd', 'rm', rbd_fq_backup_img])
        if call(['rbd', 'info', rbd_fq_snapshot]) == 0:
            call(['rbd', 'snap', 'unprotect', rbd_fq_snapshot])
            check_call(['rbd', 'snap', 'rm', rbd_fq_snapshot])

        # Remove the PV, PVC, and CM if any
        pv = meta.labels[METADATA_PREFIX + 'pv-name']
        label_selector = METADATA_PREFIX + 'pv-name=%s' % pv
        corev1.delete_collection_persistent_volume(
            label_selector=label_selector,
        )
        corev1.delete_collection_namespaced_persistent_volume_claim(
            NAMESPACE,
            label_selector=label_selector,
        )
        corev1.delete_collection_namespaced_config_map(
            NAMESPACE,
            label_selector=label_selector,
        )

        # Annotate job
        batchv1.patch_namespaced_job(
            job.metadata.name,
            job.metadata.namespace,
            {
                'metadata': {
                    'annotations': {
                        METADATA_PREFIX + 'cleaned-up': 'true',
                    },
                },
            },
        )

    return currently_backing_up


def backup_rbd_fs(api, ceph, vol, now):
    batchv1 = k8s_client.BatchV1Api(api)

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
                            image=BACKUP_IMAGE,
                            args=[
                                'restic',
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


def backup_rbd_block(api, ceph, vol, now):
    corev1 = k8s_client.CoreV1Api(api)
    batchv1 = k8s_client.BatchV1Api(api)

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

    # Turn it into an image
    check_call(['rbd', 'snap', 'protect', rbd_fq_snapshot])
    check_call(['rbd', 'clone', rbd_fq_snapshot, rbd_fq_backup_img])

    # Get layout
    layout_json = check_output([
        'rbd', 'diff', '--whole-object', '--format=json', rbd_fq_image,
    ]).decode('utf-8')

    labels = {
        METADATA_PREFIX + 'volume-type': 'rbd',
        METADATA_PREFIX + 'volume-mode': 'block',
        METADATA_PREFIX + 'pv-name': vol['pv'],
        METADATA_PREFIX + 'pvc-namespace': vol['namespace'],
        METADATA_PREFIX + 'pvc-name': vol['name'],
        METADATA_PREFIX + 'rbd-pool': vol['rbd_pool'],
        METADATA_PREFIX + 'rbd-name': vol['rbd_name'],
    }

    # Create a PersistentVolume
    pv = corev1.create_persistent_volume(k8s_client.V1PersistentVolume(
        metadata=k8s_client.V1ObjectMeta(
            name='backup-rbd-block-%s' % vol['pv'],
            labels=labels,
        ),
        spec=k8s_client.V1PersistentVolumeSpec(
            access_modes=['ReadWriteMany'],
            capacity={'storage': vol['size']},
            persistent_volume_reclaim_policy='Delete',
            storage_class_name='ceph-backup',
            volume_mode='Block',
            rbd=k8s_client.V1RBDVolumeSource(
                monitors=ceph['monitors'],
                pool=vol['rbd_pool'],
                image=rbd_backup_img,
                secret_ref=k8s_client.V1SecretReference(
                    name=ceph['secret'],
                    namespace=NAMESPACE,
                ),
                user=ceph['user'],
            ),
        ),
    ))
    logger.info("Created PersistentVolume %s", pv.metadata.name)

    # Create a PersistentVolumeClaim
    pvc = corev1.create_namespaced_persistent_volume_claim(
        NAMESPACE,
        k8s_client.V1PersistentVolumeClaim(
            metadata=k8s_client.V1ObjectMeta(
                name='backup-rbd-block-%s' % vol['pv'],
                labels=labels,
            ),
            spec=k8s_client.V1PersistentVolumeClaimSpec(
                access_modes=['ReadWriteMany'],
                resources=k8s_client.V1ResourceRequirements(
                    requests={'storage': vol['size']},
                ),
                storage_class_name='ceph-backup',
                volume_mode='Block',
                volume_name=pv.metadata.name,
            ),
        ),
    )
    logger.info("Created PersistentVolumeClaim %s", pvc.metadata.name)

    # Create a configmap to store the layout
    cm = corev1.create_namespaced_config_map(
        NAMESPACE,
        k8s_client.V1ConfigMap(
            metadata=k8s_client.V1ObjectMeta(
                name='backup-rbd-block-%s' % vol['pv'],
                labels=labels,
            ),
            data={'layout.json': layout_json},
        ),
    )
    logger.info("Created ConfigMap %s", cm.metadata.name)

    # Create a job to do the backup
    script = (
        'streaming-qcow2-writer /disk /layout/layout.json'
        + ' | restic'
        + ' -r $(URL)'
        + ' --host $(HOST)'
        + ' backup --stdin --stdin-filename disk.qcow2'
    )
    job = batchv1.create_namespaced_job(NAMESPACE, k8s_client.V1Job(
        metadata=k8s_client.V1ObjectMeta(
            generate_name='backup-rbd-block-%s-' % vol['namespace'],
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
                            image=BACKUP_IMAGE,
                            args=['sh', '-c', script],
                            env=format_env(
                                URL=('secret', RESTIC_SECRET_NAME, 'url'),
                                HOST='rbd-block-%s-nspvc-%s' % (
                                    vol['namespace'],
                                    vol['name'],
                                ),
                                RESTIC_PASSWORD=(
                                    'secret', RESTIC_SECRET_NAME, 'password',
                                ),
                            ),
                            volume_mounts=[
                                k8s_client.V1VolumeMount(
                                    mount_path='/layout',
                                    name='layout',
                                    read_only=True,
                                ),
                            ],
                            volume_devices=[
                                k8s_client.V1VolumeDevice(
                                    device_path='/disk',
                                    name='disk',
                                ),
                            ],
                        ),
                    ],
                    volumes=[
                        k8s_client.V1Volume(
                            name='disk',
                            persistent_volume_claim=(
                                k8s_client.V1PersistentVolumeClaimVolumeSource(
                                    claim_name=pvc.metadata.name,
                                )
                            ),
                        ),
                        k8s_client.V1Volume(
                            name='layout',
                            config_map=k8s_client.V1ConfigMapVolumeSource(
                                name=cm.metadata.name,
                            ),
                        ),
                    ],
                ),
            ),
        ),
    ))
    logger.info("Created job %s", job.metadata.name)

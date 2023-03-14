import argparse
from datetime import datetime
import kubernetes.client as k8s_client
import kubernetes.config as k8s_config
import logging
import math
import os
import shlex
import subprocess


logger = logging.getLogger(__name__)


METADATA_PREFIX = 'cephbackup.hpc.nyu.edu/'

ANNOTATION_ENABLED = METADATA_PREFIX + 'backup'
ANNOTATION_LAST_ATTEMPT = METADATA_PREFIX + 'last-start'

NAMESPACE = 'ceph-backup'


def parse_bool(value):
    if value is None:
        return None
    elif value.lower() in ('1', 'yes', 'true'):
        return True
    elif value.lower() in ('0', 'no', 'false'):
        return False
    else:
        return None


def parse_date(s):
    assert len(s) == 20 and s[-1] == 'Z'
    return datetime.fromisoformat(s[:-1])


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


def list_namespaces(api):
    corev1 = k8s_client.CoreV1Api(api)
    return corev1.list_namespace().items


def list_persistent_volume_claims(api):
    corev1 = k8s_client.CoreV1Api(api)
    return corev1.list_persistent_volume_claim_for_all_namespaces().items


def list_persistent_volumes(api):
    corev1 = k8s_client.CoreV1Api(api)
    return corev1.list_persistent_volume().items


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
        'secret': 'ceph',
        'user': os.environ['CEPH_USER'],
    }

    api = k8s_client.ApiClient()

    to_backup = build_list_to_backup(api, now)

    for vol in to_backup:
        if vol['mode'] == 'Filesystem':
            backup_rbd_fs(api, ceph, vol, now)
        else:
            logger.warning("Unsupported volume mode %r", vol['mode'])


def build_list_to_backup(api, now):
    # List all namespaces, collect backup configuration
    namespaces = {}
    for ns in list_namespaces(api):
        annotations = ns.metadata.annotations or {}
        namespaces[ns.metadata.name] = {
            'backup': parse_bool(annotations.get(ANNOTATION_ENABLED)),
        }
    logger.info("Got %d namespaces", len(namespaces))

    # List all PVCs, collect configuration and PV links
    claims = {}
    for pvc in list_persistent_volume_claims(api):
        annotations = pvc.metadata.annotations or {}
        claims[pvc.spec.volume_name] = {
            'backup': parse_bool(annotations.get(ANNOTATION_ENABLED)),
            'namespace': pvc.metadata.namespace,
            'name': pvc.metadata.name,
        }
    logger.info("Got %d claims", len(claims))

    # List all PVs, collect configuration
    volumes = []
    for pv in list_persistent_volumes(api):
        annotations = pv.metadata.annotations or {}
        last_backup = annotations.get(ANNOTATION_LAST_ATTEMPT)
        if last_backup:
            last_backup = parse_date(last_backup)
        if pv.spec.csi and pv.spec.csi.driver == 'rbd.csi.ceph.com':
            vol = {
                'name': pv.metadata.name,
                'backup': parse_bool(annotations.get(ANNOTATION_ENABLED)),
                'last_backup': last_backup,
                'mode': pv.spec.volume_mode,
                'size': pv.spec.capacity.get('storage'),
                'rbd_pool': pv.spec.csi.volume_attributes['pool'],
                'rbd_name': pv.spec.csi.volume_attributes['imageName'],
                'csi': {
                    'cluster_id': pv.spec.csi.volume_attributes['clusterID'],
                }
            }
            if pv.spec.csi.node_stage_secret_ref:
                vol['csi']['secret'] = (
                    pv.spec.csi.node_stage_secret_ref.namespace,
                    pv.spec.csi.node_stage_secret_ref.name,
                )
            if pv.spec.csi.fs_type:
                vol['csi']['fstype'] = pv.spec.csi.fs_type
            volumes.append(vol)
    logger.info("Got %d volumes", len(volumes))

    # Build list of RBD volumes to backup
    to_backup = []
    for pv in volumes:
        try:
            claim = claims[pv['name']]
        except KeyError:
            logger.warning(
                "PersistentVolume without a PersistentVolumeClaim: %s",
                pv['name'],
            )
            continue
        ns = namespaces[claim['namespace']]

        if claim['namespace'] == NAMESPACE:
            continue

        # Check configuration
        if pv['backup'] is False:
            continue
        elif pv['backup'] is None:
            if ns['backup'] is False:
                continue
            if claim['backup'] is False:
                continue

        to_backup.append({
            'pv': pv['name'],
            'mode': pv['mode'],
            'namespace': claim['namespace'],
            'name': claim['name'],
            'last_backup': pv['last_backup'],
            'rbd_pool': pv['rbd_pool'],
            'rbd_name': pv['rbd_name'],
            'csi': pv['csi'],
            'size': pv['size'],
        })

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

    # Label the PV
    corev1.patch_persistent_volume(vol['pv'], {
        'metadata': {
            'annotations': {
                ANNOTATION_LAST_ATTEMPT: render_date(now),
            },
        },
    })

    # Make a snapshot
    rbd_image = vol['rbd_pool'] + '/' + vol['rbd_name']
    rbd_snapshot = rbd_image + '@backup'
    check_call(['rbd', 'snap', 'create', rbd_snapshot])

    # Turn it into an image, so the filesystem can be fixed on mount
    # (if the image was in use when snapshotting, it will need repair)
    rbd_backup_img = vol['rbd_pool'] + '/' + 'backup-' + vol['rbd_name']
    check_call(['rbd', 'snap', 'protect', rbd_snapshot])
    check_call(['rbd', 'clone', rbd_snapshot, rbd_backup_img])

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
                                URL=('secret', 'restic', 'url'),
                                HOST='rbd-fs-%s--%s' % (
                                    vol['namespace'],
                                    vol['name'],
                                ),
                                RESTIC_PASSWORD=(
                                    'secret', 'restic', 'password',
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
                                image='backup-' + vol['rbd_name'],
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


# TODO: Update last-backup annotation
# TODO: Cleanup snapshots and cloned images

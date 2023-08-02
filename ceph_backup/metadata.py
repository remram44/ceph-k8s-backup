from datetime import datetime, timedelta
import kubernetes.client as k8s_client
import logging
import os


METADATA_PREFIX = 'cephbackup.hpc.nyu.edu/'

ANNOTATION_ENABLED = METADATA_PREFIX + 'backup'
ANNOTATION_LAST_ATTEMPT = METADATA_PREFIX + 'last-start'

NAMESPACE = os.environ.get('NAMESPACE', 'ceph-backup')


logger = logging.getLogger(__name__)


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


def list_namespaces(api):
    corev1 = k8s_client.CoreV1Api(api)
    return corev1.list_namespace().items


def list_persistent_volume_claims(api):
    corev1 = k8s_client.CoreV1Api(api)
    return corev1.list_persistent_volume_claim_for_all_namespaces().items


def list_persistent_volumes(api):
    corev1 = k8s_client.CoreV1Api(api)
    return corev1.list_persistent_volume().items


def list_volumes_to_backup(api):
    # List all namespaces, collect backup configuration
    namespaces = {}
    for ns in list_namespaces(api):
        annotations = ns.metadata.annotations or {}
        namespaces[ns.metadata.name] = {
            'backup': parse_bool(annotations.get(ANNOTATION_ENABLED)),
        }

    # List all PVCs, collect configuration and PV links
    claims = {}
    for pvc in list_persistent_volume_claims(api):
        annotations = pvc.metadata.annotations or {}
        last_backup = annotations.get(METADATA_PREFIX + 'last-backup')
        if last_backup:
            last_backup = parse_date(last_backup)
        claims[pvc.spec.volume_name] = {
            'backup': parse_bool(annotations.get(ANNOTATION_ENABLED)),
            'last_backup': last_backup,
            'namespace': pvc.metadata.namespace,
            'name': pvc.metadata.name,
        }

    # List all PVs, collect configuration
    volumes = []
    for pv in list_persistent_volumes(api):
        annotations = pv.metadata.annotations or {}
        last_attempt = annotations.get(ANNOTATION_LAST_ATTEMPT)
        if last_attempt:
            last_attempt = parse_date(last_attempt)
        else:
            # Don't backup a volume for 6 hours
            last_attempt = pv.metadata.creation_timestamp - timedelta(hours=18)
            if last_attempt.tzinfo is None:
                pass
            elif last_attempt.tzinfo.utcoffset(last_attempt) == timedelta(0):
                last_attempt = last_attempt.replace(tzinfo=None)
            else:
                raise AssertionError("Non-UTC creationTimestamp")
        if pv.spec.csi and pv.spec.csi.driver == 'rbd.csi.ceph.com':
            vol = {
                'name': pv.metadata.name,
                'backup': parse_bool(annotations.get(ANNOTATION_ENABLED)),
                'last_attempt': last_attempt,
                'mode': pv.spec.volume_mode,
                'size': pv.spec.capacity.get('storage'),
                'rbd_pool': pv.spec.csi.volume_attributes['pool'],
                'rbd_name': pv.spec.csi.volume_attributes['imageName'],
                'csi': {
                    'cluster_id': pv.spec.csi.volume_attributes['clusterID'],
                }
            }
            if pv.spec.csi.fs_type:
                vol['csi']['fstype'] = pv.spec.csi.fs_type
            volumes.append(vol)

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
            'last_backup': claim['last_backup'],
            'last_attempt': pv['last_attempt'],
            'rbd_pool': pv['rbd_pool'],
            'rbd_name': pv['rbd_name'],
            'csi': pv['csi'],
            'size': pv['size'],
        })

    return to_backup

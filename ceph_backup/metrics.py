import argparse
from datetime import datetime
import kubernetes.client as k8s_client
import kubernetes.config as k8s_config
import logging
import math
from prometheus_client import REGISTRY, make_wsgi_app
from prometheus_client.exposition import ThreadingWSGIServer
from prometheus_client.metrics_core import GaugeMetricFamily, \
    GaugeHistogramMetricFamily
from wsgiref.simple_server import make_server, WSGIRequestHandler

from .metadata import METADATA_PREFIX, NAMESPACE, list_volumes_to_backup


logger = logging.getLogger(__name__)


def print_table(log, table, header=None):
    # Measure fields
    table_iter = iter(table)
    if header:
        first_row = header
    else:
        try:
            first_row = next(table_iter)
        except StopIteration:
            return
    fields = [len(col) for col in first_row]
    for row in table_iter:
        if len(row) != len(fields):
            raise ValueError("Inconsistent number of columns in table")
        for col in range(len(row)):
            fields[col] = max(fields[col], len(row[col]))

    # Print table
    def print_row(row):
        line = []
        for value, size in zip(row, fields):
            line.append('{0: <{1}}'.format(value, size))
        log('  '.join(line))

    if header:
        print_row(header)
    for row in table:
        print_row(row)


def collect(show_table=False):
    now = datetime.utcnow()

    with k8s_client.ApiClient() as api:
        to_backup = list_volumes_to_backup(api)

        batchv1 = k8s_client.BatchV1Api(api)
        jobs = batchv1.list_namespaced_job(
            NAMESPACE,
            label_selector=METADATA_PREFIX + 'volume-type=rbd',
        ).items

        crons = batchv1.list_namespaced_job(
            NAMESPACE,
            label_selector='app.kubernetes.io/component=scheduler',
        ).items

    volumes_backed_up = GaugeMetricFamily(
        'volumes_backed_up',
        "Volumes that have backups enabled",
        labels=['namespace'],
    )
    volume_backup_due = GaugeHistogramMetricFamily(
        'volume_backups_due',
        "Volumes to backup by due date (in hours)",
        labels=['namespace'],
    )
    volume_backup_age = GaugeHistogramMetricFamily(
        'volume_backup_age',
        "Volumes to backup by last success age (in hours)",
        labels=['namespace'],
    )
    volume_never_backed_up = GaugeMetricFamily(
        'volume_never_backed_up',
        "Volumes that have never been backed up",
        labels=['namespace'],
    )
    running_backup_jobs = GaugeMetricFamily(
        'running_backup_jobs',
        "Number of backup jobs running now",
        labels=['namespace'],
    )
    failed_backup_jobs = GaugeMetricFamily(
        'failed_backup_jobs',
        "Number of backup jobs in failed status",
        labels=['namespace'],
    )
    failed_backup_crons = GaugeMetricFamily(
        'failed_backup_crons',
        "Number of cronjob instances in failed status",
    )

    namespaces = {}
    for vol in to_backup:
        try:
            data = namespaces[vol['namespace']]
        except KeyError:
            data = {'volumes': 0, 'due': [0] * 25, 'age': [0] * 37, 'never': 0}
            namespaces[vol['namespace']] = data

        data['volumes'] += 1

        if vol['last_attempt'] is None:
            due = 0
        else:
            due = (vol['last_attempt'] - now).total_seconds() + 24 * 3600
            due = max(0, due)
            due = math.ceil(due / 3600)
            due = min(24, due)
        data['due'][due] += 1

        if vol['last_backup'] is None:
            data['never'] += 1
        else:
            age = (now - vol['last_backup']).total_seconds()
            age = math.floor(age / 3600)
            age = min(36, age)
            data['age'][age] += 1

    for namespace, data in namespaces.items():
        volumes_backed_up.add_metric([namespace], data['volumes'])

        volume_never_backed_up.add_metric([namespace], data['never'])

        sum_value = 0
        buckets = []
        for due, value in enumerate(data['due'][:24]):
            sum_value += value
            buckets.append((str(due), sum_value))
        sum_value += data['due'][24]
        buckets.append(('+Inf', sum_value))
        volume_backup_due.add_metric([namespace], buckets, sum_value)

        sum_value = 0
        buckets = []
        for age, value in enumerate(data['age'][:36]):
            sum_value += value
            buckets.append((str(age), sum_value))
        sum_value += data['age'][36]
        buckets.append(('+Inf', sum_value))
        volume_backup_age.add_metric([namespace], buckets, sum_value)

    running_jobs = {}
    failed_jobs = {}
    backup_info = {}
    for job in jobs:
        labels = job.metadata.labels
        ns = labels[METADATA_PREFIX + 'pvc-namespace']
        pvc = labels[METADATA_PREFIX + 'pvc-name']
        if job.status.active:
            running_jobs[ns] = running_jobs.get(ns, 0) + 1
            backup_info.setdefault((ns, pvc), []).append(
                'active ' + job.metadata.name,
            )
        elif any(
            condition.type.lower() == 'failed'
            and condition.status.lower() == 'true'
            for condition in job.status.conditions or ()
        ):
            failed_jobs[ns] = failed_jobs.get(ns, 0) + 1
            backup_info.setdefault((ns, pvc), []).append(
                'failed ' + job.metadata.name,
            )
        elif not job.metadata.annotations.get(METADATA_PREFIX + 'cleaned-up'):
            backup_info.setdefault((ns, pvc), []).append(
                'ran ' + job.metadata.name,
            )
        else:
            backup_info.setdefault((ns, pvc), []).append(
                'done ' + job.metadata.name,
            )

    if show_table:
        table = []
        for vol in to_backup:
            info = backup_info.get((vol['namespace'], vol['name']), ())
            table.append((
                vol['namespace'],
                vol['name'],
                vol['last_attempt'].isoformat()
                if vol['last_attempt'] else 'none',
                vol['last_backup'].isoformat()
                if vol['last_backup'] else 'none',
                ', '.join(info),
            ))
        print_table(
            print,
            table,
            ('NAMESPACE', 'PVC', 'LAST ATTEMPT', 'LAST BACKUP', 'JOBS'),
        )

    for namespace, value in running_jobs.items():
        running_backup_jobs.add_metric([namespace], value)

    for namespace, value in failed_jobs.items():
        failed_backup_jobs.add_metric([namespace], value)

    failed_crons = 0
    for cron in crons:
        if any(
            condition.type.lower() == 'failed'
            and condition.status.lower() == 'true'
            for condition in cron.status.conditions or ()
        ):
            failed_crons += 1

    failed_backup_crons.add_metric([], failed_crons)

    return [
        volumes_backed_up,
        volume_backup_due,
        volume_backup_age,
        volume_never_backed_up,
        running_backup_jobs,
        failed_backup_jobs,
        failed_backup_crons,
    ]


class Collector(object):
    def collect(self):
        return collect()


class SilentHandler(WSGIRequestHandler):
    """WSGI handler that does not log requests."""

    def log_message(self, format, *args):
        """Log nothing."""


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        'ceph-backup-metrics',
        description="Expose metrics from ceph-backup operations",
    )
    parser.add_argument('--kubeconfig', nargs=1)
    parser.add_argument('--table', action='store_true', default=False)
    args = parser.parse_args()

    if args.kubeconfig:
        logger.info("Using specified config file")
        k8s_config.load_kube_config(args.kubeconfig[0])
    else:
        logger.info("Using in-cluster config")
        k8s_config.load_incluster_config()

    if args.table:
        collect(True)
        return

    REGISTRY.register(Collector())

    httpd = make_server(
        '0.0.0.0', 8080,
        make_wsgi_app(),
        ThreadingWSGIServer, handler_class=SilentHandler,
    )
    httpd.serve_forever()

#!/usr/bin/env python3
"""Background staging/pulling, called by qcommon.async_transfer.

Transfer configuration contains paths, never secrets. Google registry access
tokens are piped over stdin, never passed in argv or stored in queue state.
"""
import json
import pathlib
import shlex
import subprocess
import sys

from vultr_worker import verify_manifest


def ssh_args(cfg):
    return ['ssh', '-i', cfg['key'], '-o', 'IdentitiesOnly=yes',
            '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
            '-o', 'StrictHostKeyChecking=accept-new',
            '-o', 'UserKnownHostsFile=' + cfg['known_hosts']]


def transfer(mode, config):
    cfg = json.loads(pathlib.Path(config).read_text())
    ssh = ssh_args(cfg); host = 'root@' + cfg['ip']
    remote = cfg['remote']; stage = pathlib.Path(cfg['stage'])
    shell = shlex.join(ssh)
    def command(args, **kw): return subprocess.run(args, check=True, **kw)
    def remote_run(cmd, **kw): return command(ssh + [host, cmd], **kw)
    if mode == 'push':
        job = json.loads((stage / 'job.json').read_text())
        remote_run('mkdir -p ' + remote + '/inputs ' + remote + '/out', timeout=30)
        command(['rsync', '-a', '-e', shell, str(stage/'job.json'), str(stage/'worker.py'),
                 host + ':' + remote + '/'], timeout=180)
        source = pathlib.Path(cfg['inputs'])
        if cfg.get('inputs_source') == 'gcs' or not source.is_dir():
            source = stage / 'downloaded-inputs'; source.mkdir(exist_ok=True)
            sub = job['spec'].get('inputs') or ''
            bucket = cfg.get('bucket','')
            if not bucket: raise ValueError('input directory missing and no GCS bucket configured')
            command([cfg['gsutil'],'-q','rsync','-r','gs://'+bucket+'/inputs/'+sub,str(source)])
        sub = job['spec'].get('inputs') or ''
        target = remote + '/inputs/' + sub
        remote_run('mkdir -p ' + shlex.quote(target), timeout=30)
        command(['rsync', '-aL', '-e', shell, str(source) + '/', host + ':' + target + '/'])
        # Seed only unfinished tasks' prior outputs, without terminal markers.
        seed = stage / 'seed'
        if seed.exists():
            command(['rsync', '-a', '--exclude=_rc.txt', '--exclude=_started.txt',
                     '-e', shell, str(seed) + '/', host + ':' + remote + '/out/'])
        image = job['spec']['image']
        registry = image.split('/')[0]
        if registry.endswith(('.pkg.dev', '.gcr.io')) or registry == 'gcr.io':
            p = subprocess.run([cfg['gcloud'], 'auth', 'print-access-token'],
                               capture_output=True, timeout=60, check=True)
            remote_run('docker login -u oauth2accesstoken --password-stdin ' + shlex.quote(registry),
                       input=p.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        try:
            remote_run('docker pull ' + shlex.quote(image), timeout=900)
        finally:
            if registry.endswith(('.pkg.dev', '.gcr.io')) or registry == 'gcr.io':
                remote_run('docker logout ' + shlex.quote(registry), timeout=30)
        # Marker distinguishes fully staged inputs/image from an interrupted push.
        remote_run('touch ' + remote + '/staged', timeout=30)
    elif mode in ('pull', 'partial'):
        dest = pathlib.Path(cfg['dest']); dest.mkdir(parents=True, exist_ok=True)
        command(['rsync', '-a'] + (['--delete'] if mode == 'pull' else []) +
                ['-e', shell, host + ':' + remote + '/out/', str(dest / 'out') + '/'])
        names = ('status.json',) if mode == 'partial' else ('status.json','manifest.json','terminal.json')
        for name in names:
            p = subprocess.run(['rsync', '-a', '-e', shell, host + ':' + remote + '/' + name, str(dest / name)])
            if mode == 'pull' and p.returncode: raise RuntimeError('terminal metadata pull failed')
        if mode == 'pull':
            verify_manifest(dest / 'out', json.loads((dest / 'manifest.json').read_text()))
    else: raise ValueError('invalid transfer mode')


if __name__ == '__main__': transfer(sys.argv[1], sys.argv[2])

#!/usr/bin/env python3
"""VM-side Docker worker; also supplies output verification to the controller.

No cloud credentials. A systemd service and flock serialize runner recovery;
stable Docker names allow reattachment after interruption without rerunning work.
"""
import concurrent.futures
import fcntl
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time


def atomic(path, value):
    path = pathlib.Path(path)
    with tempfile.NamedTemporaryFile(mode='w',dir=path.parent,prefix='.'+path.name+'-',delete=False) as f:
        tmp = pathlib.Path(f.name)
        f.write(value if isinstance(value, str) else json.dumps(value))
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)


def capture_logs(name,out):
    with tempfile.TemporaryFile() as so,tempfile.TemporaryFile() as se:
        subprocess.run(['docker','logs',name],stdout=so,stderr=se,timeout=60)
        for stream,filename in ((so,'_stdout.txt'),(se,'_stderr.txt')):
            stream.seek(0)
            with tempfile.NamedTemporaryFile(dir=out,delete=False) as target:
                tmp=target.name
                import shutil
                shutil.copyfileobj(stream,target)
            os.replace(tmp,out/filename)


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''): h.update(b)
    return h.hexdigest()


def manifest(root):
    root = pathlib.Path(root)
    result = {}
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(files + [d for d in dirs if (pathlib.Path(base) / d).is_symlink()]):
            p = pathlib.Path(base) / name
            rel = p.relative_to(root).as_posix()
            if p.is_symlink(): result[rel] = {'link': os.readlink(p)}
            elif p.is_file(): result[rel] = {'sha256': digest(p), 'bytes': p.stat().st_size}
            else: raise ValueError('unsupported output type: ' + rel)
    return result


def verify_manifest(root, expected):
    for rel in expected:
        p = pathlib.PurePosixPath(rel)
        if p.is_absolute() or '..' in p.parts: raise ValueError('unsafe manifest path')
    if manifest(root) != expected: raise ValueError('output manifest mismatch')


def container_args(root, spec, i, name, command):
    out = '/mnt/disks/share/jobs/%s/out/task_%d' % (spec['name'], i)
    args = ['docker', 'create', '--name', name, '--restart=no',
            '--cpus=%s' % spec['cpus'], '--memory=%sm' % spec['mem_mib'],
            '--memory-swap=%sm' % spec['mem_mib'], '--init',
            '--workdir=/mnt/disks/share', '--entrypoint=/bin/bash',
            '-v', str(root / 'inputs') + ':/mnt/disks/share/inputs:ro',
            '-v', str(root / 'out' / ('task_%d' % i)) + ':' + out,
            '-e', 'OUT=' + out, '-e', 'IN=/mnt/disks/share/inputs' +
            ('/' + spec['inputs'] if spec.get('inputs') else '')]
    for var in ('OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
        args.extend(['-e', '%s=%s' % (var, spec['threads'])])
    return args + [spec['image'], '-c', command]


def docker_state(name):
    r = subprocess.run(['docker', 'inspect', '--format', '{{json .State}}', name],
                       capture_output=True, text=True, timeout=20)
    return json.loads(r.stdout) if r.returncode == 0 else None


def run(root):
    root = pathlib.Path(root).resolve()
    lock = (root / 'worker.lock').open('a')
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close(); return
    if (root / 'terminal.json').exists():
        lock.close(); return
    data = json.loads((root / 'job.json').read_text())
    spec = data['spec']; tasks = data['commands']
    status = {'state': 'RUNNING', 'tasks': {}, 'started': time.time()}
    guard = threading.Lock()

    def update(i, value):
        with guard:
            status['tasks'][str(i)] = value
            atomic(root / 'status.json', status)

    def cancelled():
        return (root / 'cancel').exists() or time.time() >= data['deadline']

    def task(i):
        out = root / 'out' / ('task_%d' % i); out.mkdir(parents=True, exist_ok=True)
        rcfile = out / '_rc.txt'
        if rcfile.is_symlink(): raise ValueError('return code cannot be a symlink')
        if rcfile.exists():
            update(i, {'state': 'DONE', 'rc': int(rcfile.read_text())}); return
        name = data['tag'] + '-' + str(i)
        command = tasks[str(i)]
        atomic(out / '_cmd.txt', command + '\n')
        started_file = out / '_started.txt'
        if started_file.is_symlink(): raise ValueError('start marker cannot be a symlink')
        if not started_file.exists(): atomic(started_file, str(time.time()))
        started = float(started_file.read_text())
        reason = ''; rc = 125
        try:
            state = docker_state(name)
            if state is None:
                if cancelled():
                    rc = 143; reason = 'cancelled before execution'
                else:
                    made = subprocess.run(container_args(root, spec, i, name, command),
                                          capture_output=True, text=True, timeout=60)
                    if made.returncode: raise RuntimeError(made.stderr[-1000:])
                    state = docker_state(name)
            if state:
                if state['Status'] == 'created':
                    subprocess.run(['docker', 'start', name], check=True,
                                   capture_output=True, timeout=30)
                update(i, {'state': 'RUNNING', 'started': started})
                last_logs = 0
                while True:
                    state = docker_state(name)
                    if state is None: raise RuntimeError('container disappeared')
                    if not state['Running']:
                        rc = state['ExitCode']
                        if state.get('OOMKilled'): reason = 'memory limit exceeded'
                        break
                    timeout = time.time() - started >= spec['timeout_sec']
                    if cancelled() or timeout:
                        subprocess.run(['docker', 'kill', name], capture_output=True, timeout=30)
                        rc = 124 if timeout else 143
                        reason = 'timeout' if timeout else 'cancelled or expiry'
                        break
                    if time.time() - last_logs >= 5:
                        capture_logs(name,out)
                        last_logs = time.time()
                    time.sleep(1)
                capture_logs(name,out)
        except Exception as e:
            reason = str(e)
            # Fail closed: stop any surviving process before marking completion.
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=30)
        atomic(out / '_reason.txt', reason)
        atomic(rcfile, str(rc) + '\n')
        update(i, {'state': 'DONE' if rc == 0 else 'FAILED', 'rc': rc,
                   'seconds': time.time() - started, 'reason': reason})

    (root / 'out').mkdir(exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=data['parallelism']) as pool:
        list(pool.map(task, data['indices']))
    atomic(root / 'manifest.json', manifest(root / 'out'))
    status['state'] = 'TERMINAL'; status['finished'] = time.time()
    atomic(root / 'status.json', status)
    atomic(root / 'terminal.json', status)
    lock.close()


if __name__ == '__main__':
    if sys.argv[1] == 'run': run(sys.argv[2])
    elif sys.argv[1] == 'verify':
        root = pathlib.Path(sys.argv[2]); verify_manifest(root / 'out', json.loads((root / 'manifest.json').read_text()))

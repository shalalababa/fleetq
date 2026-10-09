"""Vultr CPU VMs: durable allocation journal, SSH workers, verified pulls.

Only qd mutates fleet/job state. Transfer subprocesses write staging/cache only.
VMs are reserved exclusively per chunk; task slots pack CPU and RAM. Completed
VMs can be reused during the configured idle window, then are destroyed.
"""
import base64
import datetime
import ipaddress
import json
import math
import os
import pathlib
import shlex
import shutil
import sys
import time
import uuid

import qcommon as Q
from vultr_api import API, APIError, fit_slots, validate_spec
from vultr_transfer import ssh_args
from vultr_worker import verify_manifest

US_REGIONS = ('ewr', 'ord', 'dfw', 'atl', 'sea', 'lax', 'sjc', 'mia', 'hnl')
TERMINAL = ('SUCCEEDED', 'FAILED', 'CANCELLED')
DEFAULT_PLANS = {1:'voc-c-1c-2gb-25s-amd',2:'voc-c-2c-4gb-50s-amd',
                 4:'voc-c-4c-8gb-75s-amd',8:'voc-c-8c-16gb-150s-amd',
                 16:'voc-c-16c-32gb-300s-amd',32:'voc-c-32c-64gb-500s-amd'}


def choose_plan(plans, spec, cfg):
    validate_spec(spec)
    prefixes = tuple(cfg.get('Q_VULTR_ALLOWED_PREFIXES', 'voc-c-').split(','))
    if not prefixes or any(not p.startswith('voc-c-') for p in prefixes):
        raise ValueError('Only dedicated CPU Optimized (voc-c-) plans are authorized')
    regions = [r for r in cfg.get('Q_VULTR_REGIONS', ','.join(US_REGIONS)).split(',') if r in US_REGIONS]
    machine = spec.get('machine') or cfg.get('Q_VULTR_PLAN')
    ids = {p['id'] for p in plans}
    if machine and machine not in ids and machine+'-amd' in ids: machine += '-amd'
    defaults = dict(DEFAULT_PLANS)
    if cfg.get('Q_VULTR_DEFAULT_PLANS'):
        defaults = {int(k):v for k,v in (item.split(':',1) for item in cfg['Q_VULTR_DEFAULT_PLANS'].split(','))}
    candidates = []
    for p in plans:
        if not p['id'].startswith(prefixes) or (machine and p['id'] != machine): continue
        if not machine and defaults.get(p['vcpu_count']) != p['id']: continue
        if spec.get('vm_cpus') and p['vcpu_count'] != spec['vm_cpus']: continue
        if p['vcpu_count'] > int(cfg.get('Q_VULTR_CPU_CAP', 1)): continue
        slots = fit_slots(p, spec, int(cfg.get('Q_VULTR_RESERVE_MB', 256)))
        if slots < 1: continue
        for rank, region in enumerate(regions):
            if region not in p.get('locations', []): continue
            price = p.get('location_cost', {}).get(region, {}).get('hourly_cost', p['hourly_cost'])
            slots = min(slots, spec.get('ntasks', slots))
            candidates.append((price / slots, price, rank, p, region, slots))
    if not candidates: return None
    c = min(candidates, key=lambda c: c[:3])
    return dict(c[3], hourly_cost=c[1]), c[4], c[5]


class VultrSite:
    def __init__(self, cfg):
        self.cfg = cfg
        self.enabled = str(cfg.get('Q_VULTR_ENABLE', '0')) == '1'
        self.dry = os.environ.get('Q_DRY_RUN') == '1'
        self.cap = int(cfg.get('Q_VULTR_CPU_CAP', 1))
        self.max_vms = int(cfg.get('Q_VULTR_MAX_VMS', 1))
        self.base = pathlib.Path(Q.ROOT) / 'vultr'
        self.path = self.base / 'fleet.json'
        self.fleet = None; self.api = None; self.plans = []
        self.instances = {}; self.list_ok = False; self.error = ''
        self.catalog_at = 0

    def initialize(self):
        if self.fleet is not None: return
        self.base.mkdir(parents=True, exist_ok=True)
        fd=os.open(self.base.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
        if self.path.exists():
            self.fleet = Q.read_json(str(self.path))
            if (not isinstance(self.fleet, dict) or not isinstance(self.fleet.get('owner'),str)
                or not isinstance(self.fleet.get('nodes'),dict)):
                raise ValueError('Vultr fleet journal unreadable; refusing new ownership')
            required = {'state','label','plan','cpus','price','created','id','chunk'}
            if any(not isinstance(n,dict) or not required.issubset(n) for n in self.fleet['nodes'].values()):
                raise ValueError('Vultr fleet journal has incomplete node records')
        else:
            self.fleet = {'owner': 'qv-' + uuid.uuid4().hex[:16], 'nodes': {}}
            self.save()
        if not self.dry:
            self.api = API(self.cfg['Q_VULTR_API_KEY_FILE'])

    def save(self): Q.write_json_atomic(str(self.path), self.fleet)

    def expiry(self):
        value = self.cfg.get('Q_VULTR_EXPIRES_AT', '')
        if not value: return float('inf')
        d = datetime.datetime.fromisoformat(value)
        if d.tzinfo is None: raise ValueError('Vultr expiry requires explicit timezone')
        return d.timestamp()

    def used_cpus(self):
        return sum(n['cpus'] for n in (self.fleet or {}).get('nodes', {}).values()
                   if n['state'] != 'DELETED')

    def estimated_cost(self):
        return sum(math.ceil(max(1, n.get('deleted_at', time.time()) - n['created']) / 3600)
                   * n['price'] for n in (self.fleet or {}).get('nodes', {}).values()
                   if n.get('id') or n['state'] in ('CREATING','CREATE_UNKNOWN'))

    def within_budget(self, price, duration, reuse=None):
        now = time.time(); total = 0
        for n in (self.fleet or {}).get('nodes',{}).values():
            if not n.get('id') and n['state'] not in ('CREATING','CREATE_UNKNOWN'): continue
            end = n.get('deleted_at',now)
            if n is reuse: end = now + duration
            elif n['state'] not in ('IDLE','DELETED'): end = max(end,n.get('reserved_until',now))
            total += math.ceil(max(1,end-n['created'])/3600)*n['price']
        if reuse is None: total += math.ceil(duration/3600)*price
        return total <= float(self.cfg.get('Q_VULTR_BUDGET_USD',250))

    def refresh(self):
        self.list_ok = False
        if not self.enabled and not self.path.exists(): return
        try:
            self.initialize()
            if self.dry:
                self.plans = [dict(id='voc-c-1c-2gb-25s-amd', vcpu_count=1, ram=2048,
                                   hourly_cost=.038, locations=list(US_REGIONS))]
                self.list_ok = True; return
            if not self.plans or time.time() - self.catalog_at > 3600:
                self.plans = self.api.list('plans')
                self.catalog_at = time.time()
            instances = self.api.list('instances')
            self.instances = {i['id']: i for i in instances}
            known_ids={n['id'] for n in self.fleet['nodes'].values() if n.get('id')}
            unknown_labels={n['label'] for n in self.fleet['nodes'].values() if not n.get('id') and n['state'] in ('CREATING','CREATE_UNKNOWN')}
            self.fleet['untracked_owned_ids']=[i['id'] for i in instances
                  if self.fleet['owner'] in (i.get('tags') or []) and i['id'] not in known_ids and i.get('label') not in unknown_labels]
            self.restore_claims(Q.all_states())
            for n in self.fleet['nodes'].values():
                if n['state'] == 'DELETED': continue
                matches = [i for i in instances if i.get('label') == n['label']
                           and self.fleet['owner'] in i.get('tags', [])]
                if not n.get('id') and matches:
                    if len(matches) != 1: raise ValueError('duplicate Vultr ownership label')
                    n['id'] = matches[0]['id']; n['state'] = 'BOOTING'
                instance = self.instances.get(n.get('id'))
                if instance:
                    if instance.get('plan') != n['plan'] or self.fleet['owner'] not in instance.get('tags', []):
                        raise ValueError('Vultr ownership mismatch; refusing lifecycle actions')
                    if instance.get('main_ip') and instance['main_ip'] != '0.0.0.0':
                        ipaddress.ip_address(instance['main_ip']); n['ip'] = instance['main_ip']
                    n['provider_status'] = instance.get('status')
                elif n.get('id'):
                    # Confirm absence by GET before releasing capacity or retrying.
                    try: self.api.request('GET', 'instances/' + n['id'])
                    except APIError as e:
                        if e.status != 404: raise
                        n['state'] = 'DELETED'; n['deleted_at'] = time.time(); n['lost'] = True
            self.save(); self.error = ''; self.list_ok = True
        except (APIError, ValueError, OSError, KeyError, TypeError) as e:
            self.error = str(e); Q.log('VULTR refresh: ' + self.error)

    def restore_claims(self, states):
        claims = {}
        for st in states:
            for c in st.get('chunks',[]):
                if c.get('provider') != 'vultr' or c.get('collected') or c['state'] in TERMINAL: continue
                if c['node'] in claims: raise ValueError('multiple active chunks claim the same Vultr VM')
                claims[c['node']] = (st['name'],c['tag'])
        for token,n in self.fleet['nodes'].items():
            if token in claims:
                n['job'],n['chunk'] = claims[token]
                if n['state']=='IDLE': n['state']='RESERVED'
            elif n['state'] in ('BOOTING','RESERVED') and not n.get('chunk') and n.get('id'):
                # Creation succeeded before the job chunk was saved. No runner
                # can have launched: staging/launch always follows durable chunk state.
                n.update(state='IDLE', idle_since=time.time())

    def can_fit(self, spec):
        if not self.enabled or not self.list_ok: return False
        if (self.fleet or {}).get('untracked_owned_ids'): return False
        try:
            plan = choose_plan(self.plans, dict(spec, spot=0) if spec.get('site') == 'either'
                               and not spec.get('spot_explicit') else spec, self.cfg)
            if not plan: return False
            ntasks = min(spec.get('ntasks',1),plan[2]*int(self.cfg.get('Q_VULTR_TASKS_PER_SLOT',8)))
            duration = math.ceil(ntasks/plan[2])*spec['timeout_sec']+int(self.cfg.get('Q_VULTR_EXPIRY_MARGIN_SEC',3600))
            if time.time()+duration >= self.expiry(): return False
            nodes = list((self.fleet or {}).get('nodes',{}).values())
            idle = next((n for n in nodes if n['state']=='IDLE' and n['plan']==plan[0]['id']),None)
            if not self.within_budget(plan[0]['hourly_cost'],duration,idle): return False
            if idle: return True
            return (sum(n['state']!='DELETED' for n in nodes)<self.max_vms
                    and self.used_cpus()+plan[0]['vcpu_count']<=self.cap)
        except (ValueError,KeyError,TypeError): return False

    def ssh_config(self, n):
        return {'ip': n['ip'], 'key': os.path.expanduser(self.cfg['Q_VULTR_SSH_KEY']),
                'known_hosts': str(self.base / 'known_hosts')}

    def ssh(self, n, command, timeout=20):
        cfg = self.ssh_config(n)
        return Q.run_cmd(ssh_args(cfg) + ['root@' + cfg['ip'], command], timeout,
                         kind='vultr-ssh', name=n['label'])

    def cloud_init(self):
        # Docker package bootstrap contains no credentials. Password SSH disabled.
        return '#cloud-config\nssh_pwauth: false\npackage_update: true\npackages:\n - docker.io\n - rsync\n - python3\nruncmd:\n - [systemctl, enable, --now, docker]\n - [touch, /var/lib/q-vultr-ready]\n'

    def allocate(self, spec):
        if self.fleet.get('untracked_owned_ids'):
            raise ValueError('untracked owner-tagged Vultr instances require reconciliation: '+','.join(self.fleet['untracked_owned_ids']))
        plan = choose_plan(self.plans, spec, self.cfg)
        if not plan: raise ValueError('no allowed Vultr plan fits CPU/RAM/machine request')
        p, region, slots = plan
        margin = int(self.cfg.get('Q_VULTR_EXPIRY_MARGIN_SEC', 3600))
        duration = spec.get('allocation_sec', spec['timeout_sec'])
        if time.time() + duration + margin >= self.expiry():
            raise ValueError('Vultr expiry is too close for task timeout plus collection margin')
        for n in self.fleet['nodes'].values():
            if n['state'] == 'IDLE' and not n.get('chunk') and not n.get('retire') and n['plan'] == p['id']:
                if not self.clean_node(n): continue
                if not self.within_budget(n['price'],duration+margin,n):
                    raise ValueError('Vultr VM reuse exceeds estimated remaining credit')
                n['reserved_until'] = time.time()+duration+margin
                n['state'] = 'RESERVED'; n.pop('idle_since', None); self.save()
                return n, slots
        live = [n for n in self.fleet['nodes'].values() if n['state'] != 'DELETED']
        if len(live) >= self.max_vms or self.used_cpus() + p['vcpu_count'] > self.cap: return None
        if not self.within_budget(p['hourly_cost'],duration+margin):
            raise ValueError('Vultr estimated allocation cost exceeds remaining configured credit budget')
        available = self.api.request('GET', 'regions/' + region + '/availability') if not self.dry else {'available_plans':[p['id']]}
        if p['id'] not in available.get('available_plans', []):
            for alternate in self.cfg.get('Q_VULTR_REGIONS',','.join(US_REGIONS)).split(','):
                if alternate == region or alternate not in US_REGIONS or alternate not in p.get('locations',[]): continue
                a = self.api.request('GET','regions/'+alternate+'/availability')
                if p['id'] in a.get('available_plans',[]):
                    region = alternate
                    p = dict(p,hourly_cost=p.get('location_cost',{}).get(region,{}).get('hourly_cost',p['hourly_cost']))
                    break
            else: raise ValueError('Vultr plan temporarily unavailable in allowed US regions')
        if not self.within_budget(p['hourly_cost'],duration+margin):
            raise ValueError('Available Vultr region exceeds estimated remaining credit')
        token = uuid.uuid4().hex[:16]
        n = {'token': token, 'label': self.fleet['owner'] + '-' + token,
             'plan': p['id'], 'cpus': p['vcpu_count'], 'ram': p['ram'],
             'price': p['hourly_cost'], 'region': region, 'state': 'CREATING',
             'created': time.time(), 'reserved_until':time.time()+duration+margin, 'id': None, 'chunk': None}
        self.fleet['nodes'][token] = n; self.save()
        if self.dry:
            n.update(id='dry-' + token, ip='192.0.2.1', state='BOOTING'); self.save()
            return n, slots
        body = {'region': region, 'plan': p['id'], 'os_id': int(self.cfg.get('Q_VULTR_OS_ID', 2284)),
                'label': n['label'], 'hostname': n['label'], 'tags': [self.fleet['owner']],
                'sshkey_id': [pathlib.Path(self.cfg['Q_VULTR_SSH_KEY_ID_FILE']).read_text().strip()],
                'user_data': base64.b64encode(self.cloud_init().encode()).decode(),
                'backups': 'disabled', 'activation_email': False}
        try:
            instance = self.api.request('POST', 'instances', body)['instance']
            n['id'] = instance['id']; n['state'] = 'BOOTING'; self.save()
        except APIError as e:
            n['error'] = str(e)
            if not e.uncertain and e.status != 429:
                n['state'] = 'DELETED'; n['deleted_at'] = time.time()
            else: n['state'] = 'CREATE_UNKNOWN'
            self.save(); raise
        return n, slots

    def delete(self, n):
        if n['state'] == 'DELETED': return
        if not n.get('id'): return  # uncertain POST must be reconciled first
        if not self.dry:
            try:
                instance = self.api.request('GET', 'instances/' + n['id'])['instance']
                if not isinstance(instance,dict) or not {'id','label','tags','plan'}.issubset(instance):
                    raise ValueError('invalid Vultr instance response; absence not confirmed')
                if instance['id'] != n['id'] or not isinstance(instance['tags'],list):
                    raise ValueError('invalid Vultr instance identity or tags')
            except APIError as e:
                if e.status != 404: raise
                instance = None
            if instance:
                if (instance.get('label') != n['label'] or self.fleet['owner'] not in instance.get('tags', [])
                    or instance.get('plan') != n['plan'] or not n['plan'].startswith('voc-c-')):
                    raise ValueError('refusing to delete instance without matching allowed ownership')
                self.api.request('DELETE', 'instances/' + n['id'])
                n['state'] = 'DELETING'; n['delete_requested_at']=time.time(); self.save(); return
        n['state'] = 'DELETED'; n['deleted_at'] = time.time(); self.save()

    def clean_node(self,n):
        tags = n.get('cleanup_tags',[])
        if not tags or self.dry: return True
        if not n.get('ip'): return False
        for tag in tags:
            if not tag.startswith('qv-') or not tag[3:].isalnum(): raise ValueError('invalid chunk cleanup identity')
            script = ('set -euo pipefail; docker ps -aq --filter name=^/{t}- | xargs -r docker rm; '
                      'rm -rf -- /opt/q/{t}; systemctl reset-failed {t}.service 2>/dev/null || true').format(t=tag)
            command = 'bash -c ' + shlex.quote(script)
            if self.ssh(n,command).rc: return False
        n['cleanup_tags']=[]; self.save(); return True

    def stage(self, st, c, n):
        stage = self.base / 'stage' / c['tag']; stage.mkdir(parents=True, exist_ok=True)
        spec = dict(st['spec']); spec['image'] = spec.get('image') or self.cfg.get('Q_VULTR_IMAGE') or self.cfg['Q_IMAGE']
        cmds = pathlib.Path(st['cmds_path']).read_text().splitlines()
        data = {'tag': c['tag'], 'spec': spec, 'indices': c['indices'],
                'commands': {str(i):cmds[i] for i in c['indices']}, 'parallelism': c['parallelism'],
                'deadline': self.expiry() - int(self.cfg.get('Q_VULTR_EXPIRY_MARGIN_SEC', 3600))}
        Q.write_json_atomic(str(stage / 'job.json'), data)
        shutil.copy2(pathlib.Path(__file__).with_name('vultr_worker.py'), stage / 'worker.py')
        if spec.get('resumable'):
            for i in c['indices']:
                old = pathlib.Path(Q.P['cache']) / st['name'] / ('task_%d' % i)
                if old.exists(): shutil.copytree(old, stage / 'seed' / ('task_%d' % i), dirs_exist_ok=True,symlinks=True)
            # Unverified partial pulls may carry useful checkpoints, but their
            # exit codes never confer success. Seed payloads only on a retry.
            for prior in st.get('chunks',[]):
                if prior is c or prior.get('provider') != 'vultr' or prior.get('collected'): continue
                for i in set(prior['indices']) & set(c['indices']):
                    old = self.base / 'pull' / prior['tag'] / 'out' / ('task_%d' % i)
                    if old.exists(): shutil.copytree(old,stage/'seed'/('task_%d'%i),dirs_exist_ok=True,symlinks=True)
        src = pathlib.Path(self.cfg.get('Q_VULTR_INPUTS_ROOT', Q.P['inputs'])) / (spec.get('inputs') or '')
        config = dict(self.ssh_config(n), remote='/opt/q/' + c['tag'], stage=str(stage),
                      inputs=str(src), dest=str(self.base / 'pull' / c['tag']),
                      gcloud=self.cfg.get('Q_GCLOUD', 'gcloud'),gsutil=self.cfg.get('Q_GSUTIL','gsutil'),
                      bucket=self.cfg.get('Q_BUCKET',''), inputs_source=self.cfg.get('Q_VULTR_INPUTS_SOURCE','local'))
        p = self.base / (c['tag'] + '.json'); Q.write_json_atomic(str(p), config)
        c['config'] = str(p)

    def transfer(self, c, mode):
        state = c.setdefault('transfer', {'name': c['tag'] + '-' + mode})
        argv = [sys.executable, str(pathlib.Path(__file__).with_name('vultr_transfer.py')), mode, c['config']]
        phase = Q.async_transfer(state, argv, int(self.cfg.get('Q_VULTR_TRANSFER_TIMEOUT', 1800)),
                                 interval=10, kind='vultr-' + mode)
        if phase == 'done':
            # qcommon's active-worker path leaves a DONE marker; consume it now
            # so a later phase cannot mistake a prior transfer for its own.
            marker = pathlib.Path(Q.P['tmp']) / ('pull-' + state['name'] + '.json')
            if marker.exists() and Q.read_json(str(marker),{}).get('state') == 'DONE': marker.unlink()
        return phase

    def collect(self, st, c):
        dest = self.base / 'pull' / c['tag']
        m = json.loads((dest / 'manifest.json').read_text()); verify_manifest(dest / 'out', m)
        cache = pathlib.Path(Q.P['cache']) / st['name']; cache.mkdir(parents=True, exist_ok=True)
        for i in c['indices']:
            src = dest / 'out' / ('task_%d' % i)
            if src.is_symlink() or (src/'_rc.txt').is_symlink():
                raise ValueError('task output directory and return code must not be symbolic links')
            rc = int((src / '_rc.txt').read_text().strip())
            target = cache / src.name
            # Keep a previous successful result immutable, even across retries.
            if Q.read_rc(str(target)) != 0:
                tmp = cache / ('.' + src.name + '-' + c['tag'] + '.tmp')
                if tmp.exists(): shutil.rmtree(tmp)
                shutil.copytree(src, tmp, symlinks=True)
                prefix = src.name + '/'
                verify_manifest(tmp,{k[len(prefix):]:v for k,v in m.items() if k.startswith(prefix)})
                for base,dirs,files in os.walk(tmp):
                    for filename in files:
                        p = pathlib.Path(base)/filename
                        if not p.is_symlink():
                            with p.open('rb') as f: os.fsync(f.fileno())
                if target.exists():
                    old = self.base / 'attempts' / c['tag'] / target.name
                    old.parent.mkdir(parents=True, exist_ok=True)
                    if old.exists(): shutil.rmtree(old)
                    shutil.move(str(target), old)
                os.replace(tmp,target)
                fd=os.open(cache,os.O_RDONLY|os.O_DIRECTORY)
                try:os.fsync(fd)
                finally:os.close(fd)
            t = st['tasks'][str(i)]; t['rc'] = rc; t['state'] = 'DONE' if rc == 0 else 'FAILED'
        c['collected'] = True
        c['state'] = 'SUCCEEDED' if all(st['tasks'][str(i)]['rc'] == 0 for i in c['indices']) else 'FAILED'
        st['pull'] = {'files':len(m), 'bytes':sum(x.get('bytes',0) for x in m.values()),
                      'at':time.strftime('%FT%T%z'), 'verified':True}
        Q.event(st, 'Vultr chunk %s outputs verified and collected' % c['tag'])

    def advance_chunk(self, st, c):
        n = self.fleet['nodes'][c['node']]
        if c['state'] in TERMINAL: return
        if n['state'] == 'DELETED':
            c['state'] = 'FAILED'; c['reason'] = 'VM no longer exists'
            for i in c['indices']:
                if st['tasks'][str(i)]['state'] != 'DONE':
                    st['tasks'][str(i)].update(state='FAILED', rc=None)
            return
        if self.dry:
            c['state'] = 'RUNNING'; return
        remote = '/opt/q/' + c['tag']
        cancel = st.get('cancel_requested') or time.time() >= self.expiry() - int(self.cfg.get('Q_VULTR_EXPIRY_MARGIN_SEC',3600))
        boot_age = time.time()-c.get('submitted',n['created'])
        if c['state']=='BOOTING' and (cancel or (not n.get('ip') and boot_age>int(self.cfg.get('Q_VULTR_BOOT_TIMEOUT',1200)))):
            c.update(state='CANCELLED' if cancel else 'FAILED', collected=True,reason='cancelled or bootstrap timeout')
            for i in c['indices']: st['tasks'][str(i)].update(state='FAILED',rc=None)
            self.release(st,c,n,retire=True,immediate=True); return
        if not n.get('ip'): return
        if c['state'] == 'BOOTING':
            if cancel:
                c.update(state='CANCELLED', collected=True)
                self.release(st,c,n,immediate=True); return
            ready = self.ssh(n, 'test -f /var/lib/q-vultr-ready && docker info >/dev/null 2>&1')
            if ready.rc:
                if boot_age > int(self.cfg.get('Q_VULTR_BOOT_TIMEOUT',1200)):
                    c.update(state='FAILED', reason='bootstrap timeout', collected=True)
                    for i in c['indices']: st['tasks'][str(i)].update(state='FAILED', rc=None)
                    self.release(st,c,n,retire=True,immediate=True)
                return
            self.stage(st, c, n)
            c['state'] = 'STAGING'; Q.save_state(st)
        if c['state'] == 'STAGING':
            phase = self.transfer(c, 'push')
            if phase == 'done':
                if c['transfer']['pull'].get('rc') == 0:
                    c['state'] = 'STAGED'; c.pop('transfer', None)
                else:
                    c['stage_failures'] = c.get('stage_failures',0) + 1
                    if c['stage_failures'] >= int(self.cfg.get('Q_SUBMIT_MAX_FAILS',3)):
                        c.update(state='FAILED', reason='staging failed', collected=True)
                        for i in c['indices']: st['tasks'][str(i)].update(state='FAILED',rc=None)
                        self.release(st,c,n,immediate=True)
            # Let an in-progress transfer finish before cancelling or deleting.
            if cancel and phase in ('done','idle'):
                c.update(state='CANCELLED',collected=True)
                self.release(st,c,n,immediate=True)
            Q.save_state(st); return
        if c['state'] == 'STAGED':
            if cancel:
                c.update(state='CANCELLED',collected=True)
                self.release(st,c,n,immediate=True); return
            # Persist launch intent. Stable systemd name prevents duplicate starts.
            c['state'] = 'LAUNCHING'; Q.save_state(st)
        if c['state'] == 'LAUNCHING':
            if cancel:
                check = self.ssh(n, 'test -f {r}/terminal.json && echo terminal || '
                                   '(systemctl is-active --quiet {t}.service && touch {r}/cancel && echo active) || '
                                   '(test ! -f {r}/worker.lock && echo absent)'.format(r=remote,t=c['tag']))
                if check.rc: return
                if 'terminal' in check.out or 'active' in check.out:
                    c['state']='RUNNING'
                elif 'absent' in check.out:
                    c.update(state='CANCELLED',collected=True)
                    self.release(st,c,n,immediate=True)
                Q.save_state(st); return
            cmd = ('systemctl is-active --quiet {tag}.service || '
                   'test -f {remote}/terminal.json || '
                   'systemd-run --unit={tag} --property=Restart=on-failure '
                   '--property=RestartSec=5 /usr/bin/python3 {remote}/worker.py run {remote}').format(tag=c['tag'],remote=remote)
            r = self.ssh(n, cmd)
            if r.rc == 0: c['state'] = 'RUNNING'; c['launched'] = time.time()
            else: c['launch_error'] = r.err[-300:]
            Q.save_state(st); return
        if c['state'] == 'RUNNING':
            if cancel: self.ssh(n, 'touch ' + remote + '/cancel')
            r = self.ssh(n, 'cat ' + remote + '/status.json')
            if r.rc:
                c['poll_error'] = r.err[-300:]; return
            status = json.loads(r.out)
            c['remote_status'] = status; c['counts'] = {'done':sum(t.get('rc')==0 for t in status['tasks'].values()),'total':len(c['indices'])}
            for i, t in status['tasks'].items():
                if i in st['tasks'] and int(i) in c['indices']:
                    st['tasks'][i]['remote_state'] = t['state']
            if status['state'] == 'TERMINAL':
                # Wait for any partial pull to complete before the verified pull.
                if c.get('transfer'):
                    phase = self.transfer(c, 'partial')
                    if phase not in ('done','idle'): return
                    c.pop('transfer', None)
                c['state'] = 'PULLING'; Q.save_state(st)
            elif time.time() - c.get('last_partial',0) > int(self.cfg.get('Q_PULL_INTERVAL_SEC',900)):
                phase = self.transfer(c, 'partial')
                if phase == 'done':
                    c['last_partial'] = time.time(); c.pop('transfer', None)
                return
            else: return
        if c['state'] == 'PULLING':
            phase = self.transfer(c, 'pull')
            if phase == 'done' and c['transfer']['pull'].get('rc') == 0:
                self.collect(st, c)
                self.release(st,c,n)

    def release(self,st,c,n,retire=False,immediate=False):
        Q.save_state(st)  # terminal collection is durable before releasing capacity
        if c.get('config') and c['tag'] not in n.setdefault('cleanup_tags',[]):
            n['cleanup_tags'].append(c['tag'])
        n.update(state='IDLE',chunk=None,idle_since=0 if immediate else time.time(),retire=retire)
        self.save()

    def advance(self, st):
        if not self.list_ok or (not self.enabled and st.get('site')!='vultr'): return
        spec = dict(st['spec'])
        if spec.get('site') == 'either' and not spec.get('spot_explicit'): spec['spot'] = 0
        try:
            validate_spec(spec)
            for i, task in st['tasks'].items():
                cached = pathlib.Path(Q.P['cache']) / st['name'] / ('task_' + i)
                local = pathlib.Path(Q.P['localout']) / st['name'] / ('task_' + i)
                if Q.read_rc(str(cached)) == 0 or Q.read_rc(str(local)) == 0:
                    task.update(state='DONE', rc=0)
            for c in st.get('chunks', []):
                if c.get('provider') == 'vultr': self.advance_chunk(st, c)
            active = [c for c in st.get('chunks',[]) if c.get('provider')=='vultr' and c['state'] not in TERMINAL]
            maxtries = 1 + (int(self.cfg.get('Q_MAX_CHUNK_RETRIES',2)) if spec.get('resumable') else 0)
            busy = {i for c in active for i in c['indices']}
            eligible = [i for i in range(st['ntasks']) if i not in busy and st['tasks'][str(i)]['state']!='DONE'
                        and st['tasks'][str(i)].get('attempts',0)<maxtries]
            if st.get('cancel_requested'): eligible = []
            if st.get('vultr_allocation_failed'): eligible = []
            if eligible and self.enabled:
                selection = choose_plan(self.plans, dict(spec, ntasks=len(eligible)), self.cfg)
                if not selection: raise ValueError('no allowed Vultr plan fits this task and configured CPU cap')
                slots = selection[2]
                indices = eligible[:slots * int(self.cfg.get('Q_VULTR_TASKS_PER_SLOT',8))]
                allocation = self.allocate(dict(spec, allocation_sec=math.ceil(len(indices)/slots)*spec['timeout_sec']))
                if allocation:
                    n, slots = allocation
                    tag = 'qv-' + uuid.uuid4().hex[:20]
                    c = {'provider':'vultr','tag':tag,'job':tag,'node':n['token'],'instance_id':n['id'],
                         'indices':indices,'parallelism':slots,'state':'BOOTING','submitted':time.time(),'spot':0}
                    st.setdefault('chunks',[]).append(c)
                    st['site']='vultr'; st['state']='RUNNING'
                    for i in indices:
                        t=st['tasks'][str(i)]; t['attempts']=t.get('attempts',0)+1; t['state']='RUNNING'
                    Q.save_state(st)
                    n['chunk']=tag; n['job']=st['name']; self.save()
                    st['vultr_submit_failures']=0
                    Q.event(st,'Vultr allocated %s %s: %d task(s), %d slots' % (n['plan'],n['region'],len(indices),slots))
            done = sum(t['state']=='DONE' for t in st['tasks'].values())
            st['counts']={'done':done,'total':st['ntasks']}
            active = [c for c in st.get('chunks',[]) if c['state'] not in TERMINAL]
            if not active and (st.get('cancel_requested') or not eligible):
                st['state']='CANCELLED' if st.get('cancel_requested') else ('DONE' if done==st['ntasks'] else 'FAILED')
                Q.event(st,'Vultr %s: %d/%d verified successful tasks' % (st['state'],done,st['ntasks']))
        except (APIError, ValueError, OSError, KeyError, TypeError) as e:
            message = str(e)
            if st.get('vultr_error') != message: Q.event(st, 'Vultr waiting: ' + message)
            st['vultr_error'] = message
            if isinstance(e,APIError) and not e.uncertain and e.status not in (401,403,429):
                st['vultr_submit_failures']=st.get('vultr_submit_failures',0)+1
                if st['vultr_submit_failures']>=int(self.cfg.get('Q_SUBMIT_MAX_FAILS',3)):
                    # Never abandon an already-running chunk because another
                    # allocation failed. Stop further submissions and drain it.
                    st['vultr_allocation_failed']=True
                    if not any(c['state'] not in TERMINAL for c in st.get('chunks',[])):
                        st['state']='FAILED'
        Q.save_state(st)

    def cancel(self, st):
        st['cancel_requested'] = True
        Q.event(st,'Vultr cancellation requested; collecting outputs before VM cleanup')

    def maintenance(self, states):
        if not self.list_ok or not self.fleet: return
        chunks = {c['tag']:c for st in states for c in st.get('chunks',[]) if c.get('provider')=='vultr'}
        for n in self.fleet['nodes'].values():
            c = chunks.get(n.get('chunk'))
            if c and c.get('collected'):
                if c['tag'] not in n.setdefault('cleanup_tags',[]): n['cleanup_tags'].append(c['tag'])
                n.update(state='IDLE',chunk=None,idle_since=time.time()); self.save()
            if n['state'] == 'IDLE':
                expiry_near = time.time() >= self.expiry() - int(self.cfg.get('Q_VULTR_EXPIRY_MARGIN_SEC',3600))
                if n.get('retire') or expiry_near or time.time()-n.get('idle_since',0) >= int(self.cfg.get('Q_VULTR_IDLE_SEC',60)):
                    try: self.delete(n)
                    except (APIError,ValueError,KeyError,TypeError) as e: Q.log('VULTR cleanup: '+str(e))
            elif n['state']=='DELETING' and time.time()-n.get('delete_requested_at',0)>=60:
                try: self.delete(n)
                except (APIError,ValueError,KeyError,TypeError) as e: Q.log('VULTR cleanup retry: '+str(e))

    def summary(self):
        return '%d/%d vCPU, est $%.3f%s' % (self.used_cpus(), self.cap, self.estimated_cost(),
                                            ', '+self.error if self.error else '')

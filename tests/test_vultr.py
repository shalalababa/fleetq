"""Offline tests: no credentials, real cloud calls, or production Q_ROOT."""
import os
import json
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'lib'))
import qcommon as Q
from vultr_api import API, APIError, fit_slots, validate_spec


class Contracts(unittest.TestCase):
    def test_plan_policy_and_expiry(self):
        from site_vultr import choose_plan
        spec = dict(cpus=1, threads=1, mem_mib=512, timeout_sec=60, gpu=0, spot=0)
        plans = [dict(id='vc2-1c-1gb', vcpu_count=1, ram=1024, hourly_cost=.01, locations=['ewr']),
                 dict(id='voc-c-1c-2gb-25s-amd', vcpu_count=1, ram=2048,
                      hourly_cost=.038, locations=['ewr'])]
        cfg = {'Q_VULTR_ALLOWED_PREFIXES':'voc-c-', 'Q_VULTR_REGIONS':'ewr',
               'Q_VULTR_CPU_CAP':'1', 'Q_VULTR_RESERVE_MB':'256'}
        p, region, slots = choose_plan(plans, spec, cfg)
        self.assertEqual(p['id'], 'voc-c-1c-2gb-25s-amd')
        self.assertEqual(slots, 1)
        self.assertIsNone(choose_plan(plans, dict(spec, machine='vc2-1c-1gb'), cfg))

    def test_preferred_size_alias_and_manual_variant(self):
        from site_vultr import choose_plan
        spec=dict(cpus=1,threads=1,mem_mib=256,timeout_sec=60,spot=0,gpu=0,vm_cpus=8)
        cfg=dict(Q_VULTR_CPU_CAP='32',Q_VULTR_REGIONS='ewr')
        plans=[dict(id='voc-c-8c-16gb-150s-amd',vcpu_count=8,ram=16384,hourly_cost=.219,locations=['ewr']),
               dict(id='voc-c-8c-16gb-300s-amd',vcpu_count=8,ram=16384,hourly_cost=.247,locations=['ewr'])]
        self.assertEqual(choose_plan(plans,spec,cfg)[0]['id'],plans[0]['id'])
        self.assertEqual(choose_plan(plans,dict(spec,machine='voc-c-8c-16gb-300s'),cfg)[0]['id'],plans[1]['id'])

    def test_output_manifest_detects_corruption(self):
        from vultr_worker import manifest, verify_manifest
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td); (p / 'task_0').mkdir()
            f = p / 'task_0' / 'data'; f.write_text('result')
            m = manifest(p)
            verify_manifest(p, m)
            f.write_text('corrupt')
            with self.assertRaises(ValueError): verify_manifest(p, m)

    def test_manifest_rejects_path_traversal(self):
        from vultr_worker import verify_manifest
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError): verify_manifest(td, {'../secret': {}})

    def test_container_limits_and_paths(self):
        from vultr_worker import container_args
        spec = dict(name='demo', cpus=1, threads=1, mem_mib=512,
                    inputs='package', image='python:3.11-slim')
        args = container_args(pathlib.Path('/opt/q/test'), spec, 3, 'test-3', 'echo ok')
        self.assertIn('--memory-swap=512m', args)
        self.assertIn('--cpus=1', args)
        self.assertIn('IN=/mnt/disks/share/inputs/package', args)
        self.assertIn('OUT=/mnt/disks/share/jobs/demo/out/task_3', args)

    def test_memory_and_cpu_packing(self):
        plan = dict(vcpu_count=8, ram=16384)
        self.assertEqual(fit_slots(plan, dict(cpus=2, mem_mib=4096), 256), 3)
        self.assertEqual(fit_slots(plan, dict(cpus=1, mem_mib=512), 256), 8)

    def test_unsupported_and_invalid_resources(self):
        spec = dict(cpus=1, threads=1, mem_mib=512, timeout_sec=60, gpu=0, spot=0)
        validate_spec(spec)
        for key, value in [('cpus', 0), ('cpus', float('nan')), ('mem_mib', -1),
                           ('gpu', 1), ('spot', 1), ('inputs', '../escape')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_spec(dict(spec, **{key: value}))

    def test_secret_file_must_be_private(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / 'key'; p.write_text('test-secret')
            p.chmod(0o644)
            with self.assertRaises(ValueError): API(str(p))
            p.chmod(0o600)
            self.assertNotIn('test-secret', repr(API(str(p))))

    def test_malformed_pagination_fails_as_api_error(self):
        api=object.__new__(API)
        for response in ([],None,{'instances':[],'meta':None},{'instances':[None]}):
            with mock.patch.object(api,'request',return_value=response):
                with self.assertRaises(APIError):api.list('instances')

    def test_pagination_retains_all_pages(self):
        api=object.__new__(API)
        pages=[{'instances':[{'id':'one'}],'meta':{'links':{'next':'https://api.vultr.com/v2/instances?cursor=abc'}}},
               {'instances':[{'id':'two'}]}]
        with mock.patch.object(api,'request',side_effect=pages):
            self.assertEqual([x['id'] for x in api.list('instances')],['one','two'])


class Backend(unittest.TestCase):
    def setUp(self):
        from site_vultr import VultrSite
        self.tmp = tempfile.TemporaryDirectory()
        self.old_root, self.old_p = Q.ROOT, Q.P.copy()
        Q.ROOT = self.tmp.name
        Q.P = {k: str(pathlib.Path(self.tmp.name) / pathlib.Path(v).name) for k,v in self.old_p.items()}
        Q.ensure_tree()
        self.cfg = {'Q_VULTR_ENABLE':'1','Q_VULTR_CPU_CAP':'1','Q_VULTR_MAX_VMS':'1',
                    'Q_VULTR_ALLOWED_PREFIXES':'voc-c-','Q_VULTR_REGIONS':'ewr',
                    'Q_VULTR_SSH_KEY_ID_FILE':str(pathlib.Path(self.tmp.name)/'ssh-id'),
                    'Q_VULTR_EXPIRES_AT':'2099-01-01T00:00:00+00:00'}
        pathlib.Path(self.cfg['Q_VULTR_SSH_KEY_ID_FILE']).write_text('ssh-id')
        self.site = VultrSite(self.cfg); self.site.base.mkdir()
        self.site.fleet={'owner':'q-owner','nodes':{}}; self.site.list_ok=True
        self.site.api=mock.Mock()
        self.site.plans=[dict(id='voc-c-1c-2gb-25s-amd',vcpu_count=1,ram=2048,
                             hourly_cost=.038,locations=['ewr'])]
        self.spec=dict(name='demo',cpus=1,threads=1,mem_mib=512,timeout_sec=60,
                       gpu=0,spot=0,site='vultr',resumable=1,ntasks=1)

    def tearDown(self):
        Q.ROOT, Q.P = self.old_root, self.old_p
        self.tmp.cleanup()

    def test_uncertain_create_reserves_capacity(self):
        self.site.api.request.side_effect=[{'available_plans':[self.site.plans[0]['id']]},
                                           APIError('POST','instances',0,'timeout')]
        with self.assertRaises(APIError): self.site.allocate(self.spec)
        self.assertEqual(self.site.used_cpus(),1)
        self.assertIsNone(self.site.allocate(self.spec))
        self.assertEqual(self.site.api.request.call_count,2)

    def test_backups_disabled_on_create(self):
        self.site.api.request.side_effect=[{'available_plans':[self.site.plans[0]['id']]}, {'instance':{'id':'test-id'}}]
        self.site.allocate(self.spec)
        body=self.site.api.request.call_args.args[2]
        self.assertEqual(body['backups'],'disabled')
        self.assertNotIn('api-key',json.dumps(body))

    def test_no_delete_for_mismatched_ownership(self):
        n=dict(id='not-ours',label='q-label',state='IDLE',plan=self.site.plans[0]['id'])
        self.site.api.request.return_value={'instance':dict(label='q-label',tags=['other-owner'],plan=n['plan'])}
        with self.assertRaises(ValueError): self.site.delete(n)
        self.assertEqual(self.site.api.request.call_count,1)

    def test_successful_cache_is_not_rerun(self):
        out=pathlib.Path(Q.P['cache'])/'demo/task_0'; out.mkdir(parents=True)
        (out/'_rc.txt').write_text('0\n')
        st=dict(name='demo',spec=self.spec,ntasks=1,state='PENDING',site=None,chunks=[],
                tasks={'0':{'state':'PENDING','attempts':0}})
        self.site.advance(st)
        self.assertEqual(st['state'],'DONE')
        self.site.api.request.assert_not_called()

    def test_either_does_not_claim_full_vultr_site(self):
        self.site.fleet['nodes']['busy']={'state':'RUNNING','cpus':1,'plan':self.site.plans[0]['id'],'chunk':'busy'}
        self.assertFalse(self.site.can_fit(dict(self.spec,site='either')))

    def test_expiry_also_blocks_idle_vm_reuse(self):
        self.site.cfg['Q_VULTR_EXPIRES_AT']='2000-01-01T00:00:00+00:00'
        self.site.fleet['nodes']['idle']={'state':'IDLE','cpus':1,'plan':self.site.plans[0]['id'],'chunk':None}
        with self.assertRaises(ValueError): self.site.allocate(self.spec)

    def test_successful_collection_survives_repeat(self):
        from vultr_worker import manifest, atomic
        st=dict(name='demo',tasks={'0':{'state':'RUNNING'}},events=[])
        c=dict(tag='c1',indices=[0],state='PULLING')
        dest=self.site.base/'pull/c1'; out=dest/'out/task_0'; out.mkdir(parents=True)
        (out/'_rc.txt').write_text('0\n'); (out/'result').write_text('durable')
        atomic(dest/'manifest.json',manifest(dest/'out'))
        self.site.collect(st,c); self.site.collect(st,c)
        self.assertEqual((pathlib.Path(Q.P['cache'])/'demo/task_0/result').read_text(),'durable')
        self.assertTrue(c['collected']); self.assertEqual(c['state'],'SUCCEEDED')

    def test_transfer_phases_use_distinct_completion_markers(self):
        c={'tag':'chunk','config':'/tmp/config'}
        names=[]
        def finished(state,*args,**kwargs): names.append(state['name']); return 'done'
        with mock.patch.object(Q,'async_transfer',side_effect=finished):
            self.site.transfer(c,'push'); c.pop('transfer')
            self.site.transfer(c,'pull')
        self.assertNotEqual(names[0],names[1])

    def test_claim_restored_before_vm_can_be_reused(self):
        n={'state':'BOOTING','id':'vm','chunk':None,'plan':self.site.plans[0]['id'],'cpus':1}
        self.site.fleet['nodes']['node']=n
        c={'provider':'vultr','node':'node','tag':'chunk','state':'BOOTING'}
        self.site.restore_claims([dict(name='original',chunks=[c])])
        self.assertEqual(n['chunk'],'chunk')
        self.assertIsNone(self.site.allocate(self.spec))

    def test_unclaimed_created_vm_becomes_cleanup_eligible(self):
        n={'state':'BOOTING','id':'vm','chunk':None}
        self.site.fleet['nodes']['node']=n
        self.site.restore_claims([])
        self.assertEqual(n['state'],'IDLE')

    def test_rejected_creation_costs_nothing(self):
        self.site.fleet['nodes']['failed']={'state':'DELETED','id':None,'created':time.time(),
                                          'deleted_at':time.time(),'price':.038}
        self.assertEqual(self.site.estimated_cost(),0)

    def test_idle_vm_cannot_bypass_credit_budget(self):
        n=dict(state='IDLE',id='vm',chunk=None,plan=self.site.plans[0]['id'],cpus=1,
               price=.038,created=time.time()-3600)
        self.site.fleet['nodes']['idle']=n
        self.site.cfg['Q_VULTR_BUDGET_USD']='0.04'
        self.assertFalse(self.site.can_fit(self.spec))
        with self.assertRaises(ValueError):self.site.allocate(self.spec)

    def test_other_plan_families_require_new_authorization(self):
        from site_vultr import choose_plan
        with self.assertRaises(ValueError):choose_plan(self.site.plans,self.spec,dict(self.cfg,Q_VULTR_ALLOWED_PREFIXES=''))

    def test_empty_instance_response_does_not_release_vm(self):
        n=dict(id='vm',label='owned',state='IDLE',plan=self.site.plans[0]['id'])
        self.site.api.request.return_value={'instance':{}}
        with self.assertRaises(ValueError):self.site.delete(n)
        self.assertEqual(n['state'],'IDLE')

    def test_cancel_before_ip_assignment_releases_unstarted_vm(self):
        n=dict(id='vm',label='owned',state='BOOTING',chunk='tag',created=time.time())
        self.site.fleet['nodes']['node']=n
        c=dict(node='node',tag='tag',state='BOOTING',indices=[0])
        st=dict(name='demo',cancel_requested=True,tasks={'0':{'state':'RUNNING'}})
        self.site.advance_chunk(st,c)
        self.assertEqual(c['state'],'CANCELLED');self.assertTrue(c['collected']);self.assertTrue(n['retire'])

    def test_unknown_creation_reserves_full_projected_spend(self):
        self.site.fleet['nodes']['unknown']=dict(id=None,state='CREATE_UNKNOWN',created=time.time(),
                  reserved_until=time.time()+7200,price=.038,chunk=None)
        self.site.cfg['Q_VULTR_BUDGET_USD']='0.10'
        self.assertFalse(self.site.within_budget(.038,3600))

    def test_interrupted_copy_never_publishes_success_marker(self):
        from vultr_worker import manifest,atomic
        import site_vultr as V
        st=dict(name='demo',tasks={'0':{'state':'RUNNING'}},events=[])
        c=dict(tag='c1',indices=[0],state='PULLING')
        dest=self.site.base/'pull/c1';out=dest/'out/task_0';out.mkdir(parents=True)
        (out/'_rc.txt').write_text('0\n');(out/'payload').write_text('complete')
        atomic(dest/'manifest.json',manifest(dest/'out'))
        def interrupted(src,dst,**kwargs):
            pathlib.Path(dst).mkdir(parents=True);(pathlib.Path(dst)/'_rc.txt').write_text('0\n')
            raise OSError('simulated process interruption')
        with mock.patch.object(V.shutil,'copytree',side_effect=interrupted):
            with self.assertRaises(OSError):self.site.collect(st,c)
        self.assertNotEqual(Q.read_rc(str(pathlib.Path(Q.P['cache'])/'demo/task_0')),0)
        self.assertFalse(c.get('collected',False))

    def test_mature_vm_transient_readiness_error_keeps_attempt_alive(self):
        n=dict(id='vm',label='owned',state='RESERVED',chunk='tag',created=time.time()-86400,ip='192.0.2.1')
        self.site.fleet['nodes']['node']=n
        c=dict(node='node',tag='tag',state='BOOTING',indices=[0],submitted=time.time())
        st=dict(name='demo',tasks={'0':{'state':'RUNNING'}})
        with mock.patch.object(self.site,'ssh',return_value=Q.CallResult(255,'','transient')):
            self.site.advance_chunk(st,c)
        self.assertEqual(c['state'],'BOOTING');self.assertEqual(n['chunk'],'tag')

    def test_disabling_admission_keeps_cleanup_enabled(self):
        n=dict(id='vm',token='node',label='owned',state='IDLE',chunk=None,created=time.time(),
               cpus=1,price=.038,plan=self.site.plans[0]['id'],idle_since=0)
        self.site.fleet['nodes']['node']=n;self.site.save();self.site.enabled=False
        instance=dict(id='vm',label='owned',tags=['q-owner'],plan=n['plan'],main_ip='192.0.2.1')
        self.site.api.list.side_effect=[self.site.plans,[instance]]
        self.site.api.request.side_effect=[{'instance':instance},{}]
        self.site.refresh();self.site.maintenance([])
        self.assertEqual(n['state'],'DELETING')
        self.assertFalse(self.site.can_fit(self.spec))

    def test_seed_preserves_symlink_without_reading_controller_target(self):
        n=dict(ip='192.0.2.1')
        self.site.cfg.update(Q_VULTR_SSH_KEY='/tmp/key',Q_IMAGE='ubuntu:22.04')
        secret=pathlib.Path(self.tmp.name)/'controller-only';secret.write_text('never-upload')
        old=pathlib.Path(Q.P['cache'])/'demo/task_0';old.mkdir(parents=True)
        (old/'link').symlink_to(secret)
        commands=pathlib.Path(self.tmp.name)/'commands';commands.write_text('true\n')
        st=dict(name='demo',spec=self.spec,cmds_path=str(commands),chunks=[])
        c=dict(tag='tag',indices=[0],parallelism=1)
        self.site.stage(st,c,n)
        seeded=self.site.base/'stage/tag/seed/task_0/link'
        self.assertTrue(seeded.is_symlink())


class Transfer(unittest.TestCase):
    def test_final_pull_removes_files_deleted_after_partial_pull(self):
        import vultr_transfer as V
        from vultr_worker import manifest,atomic
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            root=pathlib.Path(td);remote=root/'remote';dest=root/'pull'
            (remote/'out/task_0').mkdir(parents=True);(dest/'out/task_0').mkdir(parents=True)
            (remote/'out/task_0/result').write_text('final')
            (dest/'out/task_0/temporary').write_text('deleted-on-worker')
            atomic(remote/'manifest.json',manifest(remote/'out'))
            atomic(remote/'status.json',{'state':'TERMINAL'});atomic(remote/'terminal.json',{})
            cfg=dict(ip='192.0.2.1',key='/tmp/key',known_hosts='/tmp/hosts',remote=str(remote),
                     stage=str(root/'stage'),dest=str(dest))
            atomic(root/'config.json',cfg)
            real_run=subprocess.run
            def local_rsync(argv,**kw):
                args=[];i=0
                while i<len(argv):
                    if argv[i]=='-e':i+=2;continue
                    args.append(argv[i].split(':',1)[1] if argv[i].startswith('root@') else argv[i]);i+=1
                return real_run(args,**kw)
            with mock.patch.object(V.subprocess,'run',side_effect=local_rsync):V.transfer('pull',str(root/'config.json'))
            self.assertFalse((dest/'out/task_0/temporary').exists())


class Worker(unittest.TestCase):
    def execute(self, timeout=False):
        import vultr_worker as W
        from subprocess import CompletedProcess
        with tempfile.TemporaryDirectory() as td:
            root=pathlib.Path(td); (root/'inputs').mkdir()
            spec=dict(name='demo',cpus=1,threads=1,mem_mib=64,timeout_sec=.001 if timeout else 60,
                      image='ubuntu:22.04',inputs=None)
            W.atomic(root/'job.json',dict(spec=spec,commands={'0':'echo hello'},tag='qtest',indices=[0],parallelism=1,deadline=time.time()+60))
            state={}
            def docker(args, **kwargs):
                op=args[1]
                if op=='inspect':
                    if not state: return CompletedProcess(args,1,'','missing')
                    return CompletedProcess(args,0,json.dumps(state),'')
                if op=='create': state.update(Status='created',Running=False,ExitCode=0)
                elif op=='start': state.update(Status='running' if timeout else 'exited',Running=timeout,ExitCode=0)
                elif op=='kill': state.update(Status='exited',Running=False,ExitCode=137)
                elif op=='logs': kwargs['stdout'].write(b'hello\n')
                return CompletedProcess(args,0,'','')
            with mock.patch.object(W.subprocess,'run',side_effect=docker): W.run(root)
            terminal=json.loads((root/'terminal.json').read_text())
            W.verify_manifest(root/'out',json.loads((root/'manifest.json').read_text()))
            return int((root/'out/task_0/_rc.txt').read_text()),terminal

    def test_worker_writes_verified_success(self):
        rc,status=self.execute()
        self.assertEqual(rc,0); self.assertEqual(status['state'],'TERMINAL')

    def test_worker_timeout_kills_and_records_124(self):
        rc,status=self.execute(timeout=True)
        self.assertEqual(rc,124)


if __name__ == '__main__': unittest.main()

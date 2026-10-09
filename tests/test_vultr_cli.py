"""CLI/daemon regressions in disposable Q_ROOTs. No cloud requests."""
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

QUEUE = pathlib.Path(__file__).resolve().parents[1]


class CLI(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=pathlib.Path(self.tmp.name)
        self.env=dict(os.environ,Q_ROOT=str(self.root),Q_DRY_RUN='1',Q_VULTR_ENABLE='1',
                      Q_CYCLE_SEC='1',Q_RSS_POLL_SEC='1',Q_LOCAL_RESERVE_PCT='0')
        self.cmds=self.root/'commands'; self.cmds.write_text('echo ok > "$OUT/result"\n')

    def tearDown(self): self.tmp.cleanup()

    def call(self,*args):
        return subprocess.run([str(QUEUE/'bin'/'q'),*args],env=self.env,capture_output=True,text=True,timeout=10)

    def submit(self,name,site,*extra):
        return self.call('submit','--name',name,'--cmds',str(self.cmds),'--site',site,
                         '--cpus','1','--mem','64M','--timeout','1m',*extra)

    def test_vultr_default_ondemand_and_admission(self):
        r=self.submit('vultr-dry','vultr');self.assertEqual(r.returncode,0,r.stderr)
        subprocess.run([str(QUEUE/'bin'/'qd'),'--once'],env=self.env,check=True,capture_output=True,timeout=10)
        s=json.loads((self.root/'state/vultr-dry.json').read_text())
        self.assertEqual(s['site'],'vultr');self.assertEqual(s['spec']['spot'],0)
        self.assertEqual(len(s['chunks']),1)
        self.assertEqual(s['chunks'][0]['parallelism'],1)

    def test_spot_rejection_does_not_poison_name(self):
        self.assertNotEqual(self.submit('vultr-spot','vultr','--spot','1').returncode,0)
        self.assertEqual(self.submit('vultr-spot','vultr','--spot','0').returncode,0)

    def test_existing_sites_keep_default_spot(self):
        for site in ('local','batch','vast','either'):
            extra=('--vast-any',) if site=='vast' else ()   # vast jobs must be pinned or say --vast-any
            r=self.submit('original-'+site,site,*extra);self.assertEqual(r.returncode,0,r.stderr)
            s=json.loads((self.root/('spool/pending/original-'+site+'.json')).read_text())
            self.assertEqual(s['spot'],1)

    def test_cancel_is_persisted_for_vultr(self):
        self.submit('vultr-cancel','vultr')
        subprocess.run([str(QUEUE/'bin'/'qd'),'--once'],env=self.env,check=True,capture_output=True,timeout=10)
        self.assertEqual(self.call('cancel','vultr-cancel').returncode,0)
        subprocess.run([str(QUEUE/'bin'/'qd'),'--once'],env=self.env,check=True,capture_output=True,timeout=10)
        s=json.loads((self.root/'state/vultr-cancel.json').read_text())
        self.assertTrue(s['cancel_requested'])
        self.assertFalse((self.root/'spool/cancel/vultr-cancel').exists())


if __name__=='__main__':unittest.main()

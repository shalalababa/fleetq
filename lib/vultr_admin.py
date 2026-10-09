#!/usr/bin/env python3
"""q vultr doctor|plans|fleet. Lifecycle mutations belong to qd."""
import json
import pathlib
import sys

import qcommon as Q
from site_vultr import VultrSite, US_REGIONS
from vultr_api import API


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else 'help'
    cfg = Q.load_config()
    if command == 'fleet':
        path = pathlib.Path(Q.ROOT) / 'vultr/fleet.json'
        print(path.read_text() if path.exists() else 'No Vultr allocations recorded.')
        return
    if command not in ('doctor','plans'):
        print('q vultr doctor | plans | fleet\nSubmit with q submit --site vultr; cancel with q cancel NAME.')
        return
    api = API(cfg['Q_VULTR_API_KEY_FILE'])
    if command == 'plans':
        for p in api.list('plans'):
            if p['id'].startswith(tuple(cfg.get('Q_VULTR_ALLOWED_PREFIXES','voc-c-').split(','))):
                regions = [r for r in p['locations'] if r in US_REGIONS]
                print('%s  %s vCPU  %s MiB  $%s/h  %s' %
                      (p['id'],p['vcpu_count'],p['ram'],p['hourly_cost'],','.join(regions)))
    else:
        instances = api.list('instances')
        print('Authenticated API: OK (%d existing instances)' % len(instances))
        print('SSH private key: %s' % ('present' if pathlib.Path(cfg['Q_VULTR_SSH_KEY']).is_file() else 'MISSING'))
        print('SSH key registration: %s' % ('present' if pathlib.Path(cfg['Q_VULTR_SSH_KEY_ID_FILE']).is_file() else 'MISSING'))
        print('Host OS id: %s; automatic backups: disabled' % cfg['Q_VULTR_OS_ID'])
        print('Regions: %s; CPU cap: %s; VM cap: %s' % (cfg['Q_VULTR_REGIONS'],cfg['Q_VULTR_CPU_CAP'],cfg['Q_VULTR_MAX_VMS']))
        print('Expiry: %s; configured credit budget: $%s' % (cfg['Q_VULTR_EXPIRES_AT'],cfg['Q_VULTR_BUDGET_USD']))


if __name__ == '__main__':
    try: main()
    except Exception as e: sys.exit(str(e))

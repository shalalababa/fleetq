"""Small Vultr v2 client. Never log credentials, HTTP bodies, or API responses.

POST is deliberately not retried: instance creation has no documented
idempotency key. The backend reconciles its unique label after uncertain calls.
"""
import json
import math
import os
import pathlib
import stat
import urllib.error
import urllib.parse
import urllib.request


class APIError(RuntimeError):
    def __init__(self, method, path, status=0, message='request failed'):
        self.status = status
        self.uncertain = status == 0 or status >= 500
        super().__init__('Vultr %s %s: HTTP %s %s' % (method, path, status, message))


class API:
    base = 'https://api.vultr.com/v2/'

    def __init__(self, key_file, timeout=25):
        p = pathlib.Path(key_file).expanduser()
        if p.is_symlink() or stat.S_IMODE(p.stat().st_mode) & 0o077:
            raise ValueError('Vultr key must be a private regular file (0600)')
        if not p.is_file() or p.stat().st_uid != os.getuid():
            raise ValueError('Vultr key must be owned by the controller user')
        self._key = p.read_text().strip()
        if not self._key: raise ValueError('Vultr key file is empty')
        self.timeout = timeout

    def request(self, method, path, data=None):
        if path.startswith('/') or '://' in path or '..' in path:
            raise ValueError('relative API endpoint required')
        req = urllib.request.Request(self.base + path,
              data=None if data is None else json.dumps(data).encode(), method=method,
              headers={'Authorization': 'Bearer ' + self._key,
                       'Content-Type': 'application/json', 'User-Agent': 'q-vultr/1'})
        # Do not forward Authorization through unexpected redirects.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs): return None
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=self.timeout) as r:
                body = r.read()
                value = json.loads(body) if body else {}
                if not isinstance(value,dict): raise ValueError('non-object API response')
                return value
        except urllib.error.HTTPError as e:
            message = 'request rejected'
            try:
                error = json.loads(e.read())
                if isinstance(error,dict): message = str(error.get('error', message))[:300]
            except (ValueError, OSError): pass
            raise APIError(method, path, e.code, message.replace(self._key, '[redacted]')) from None
        except (OSError, ValueError) as e:
            raise APIError(method, path, 0, type(e).__name__) from None

    def list(self, endpoint, key=None):
        key = key or endpoint
        path = endpoint + '?per_page=500'
        rows, seen = [], set()
        while path:
            if path in seen: raise APIError('GET', endpoint, 0, 'pagination loop')
            seen.add(path)
            data = self.request('GET', path)
            if not isinstance(data,dict): raise APIError('GET',endpoint,0,'invalid response object')
            page = data.get(key)
            if not isinstance(page, list) or any(not isinstance(row,dict) for row in page):
                raise APIError('GET', endpoint, 0, 'invalid list')
            rows.extend(page)
            meta = data.get('meta',{})
            if not isinstance(meta,dict) or not isinstance(meta.get('links',{}),dict):
                raise APIError('GET',endpoint,0,'invalid pagination metadata')
            link = meta.get('links',{}).get('next')
            if link:
                if not isinstance(link,str): raise APIError('GET',endpoint,0,'invalid pagination link')
                url = urllib.parse.urlparse(link)
                if url.netloc and url.netloc != 'api.vultr.com':
                    raise APIError('GET', endpoint, 0, 'invalid pagination host')
                query = urllib.parse.parse_qs(url.query)
                cursor = query.get('cursor', [None])[0]
                if not cursor: raise APIError('GET', endpoint, 0, 'invalid pagination cursor')
                path = endpoint + '?per_page=500&cursor=' + urllib.parse.quote(cursor, safe='')
            else: path = None
        return rows


def validate_spec(spec):
    for key in ('cpus', 'threads', 'mem_mib', 'timeout_sec'):
        v = float(spec[key])
        if not math.isfinite(v) or v <= 0:
            raise ValueError('Vultr %s must be finite and positive' % key)
    if spec.get('gpu'): raise ValueError('Vultr backend supports CPU jobs only')
    if spec.get('spot'): raise ValueError('Vultr CPU spot is unavailable; use --spot 0')
    if spec.get('vm_cpus',0)<0: raise ValueError('Vultr VM CPU count must be positive')
    if int(spec['mem_mib'])<6: raise ValueError('Docker requires at least 6 MiB memory')
    sub = spec.get('inputs') or ''
    if pathlib.PurePosixPath(sub).is_absolute() or '..' in pathlib.PurePosixPath(sub).parts:
        raise ValueError('Vultr --inputs must be a relative subpath without ..')


def fit_slots(plan, spec, reserve_mb):
    return max(0, min(int(float(plan['vcpu_count']) // float(spec['cpus'])),
                      int((int(plan['ram']) - reserve_mb) // int(spec['mem_mib']))))

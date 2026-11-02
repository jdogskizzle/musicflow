#!/usr/bin/env python3
"""musicflow web app: the same pipeline as the CLI, driven from a browser.

Serves static/index.html and a small JSON API. A background worker runs the
download/import queue (shared with the CLI through state/jobs.json), and a
search pool runs slskd searches a few at a time (their starts are spaced out).
"""
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import musicflow as mf  # noqa: E402
import formlogin  # noqa: E402
import formlogin.httpserver  # noqa: E402

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
PORT = int(os.environ.get('PORT', '8000'))

# ---------------------------------------------------------------- login
# Set the password with `python -m formlogin passwd`, or MUSICFLOW_AUTH=off (see README).
LOGIN = formlogin.Login.from_env(
    'MUSICFLOW', 'musicflow', mf.STATE, title='musicflow',
    accent=('#4f5df0', '#7480ff'),
    icon="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><circle cx='12' cy='12' r='11' fill='%235b6cff'/><path d='M10 16.5a2 2 0 1 1-1-1.7V7l7-1.5v8a2 2 0 1 1-1-1.7V8.3L10 9.4z' fill='white'/></svg>")

cfg = mf.load_config()
lid = mf.Lidarr(cfg['lidarr_url'], cfg['lidarr_key'])
slsk = mf.Slskd(cfg['slskd_url'], cfg['slskd_key']) if cfg.get('slskd_key') else None
EXTS = mf.LOSSLESS


# ---------------------------------------------------------------- Lidarr cache

class Cache:
    def __init__(self, ttl):
        self.ttl, self.at, self.value, self.lock = ttl, 0, None, threading.Lock()

    def get(self, fn):
        with self.lock:
            if self.value is None or time.time() - self.at > self.ttl:
                self.value, self.at = fn(), time.time()
            return self.value

    def clear(self):
        self.at = 0


artists_cache = Cache(60)
albums_cache = Cache(60)


def all_artists():
    return artists_cache.get(lambda: lid.req('GET', 'artist'))


def all_albums():
    def load():
        slim = []
        for a in lid.req('GET', 'album'):
            slim.append({k: a.get(k) for k in ('id', 'artistId', 'title', 'releaseDate', 'monitored', 'albumType',
                                               'secondaryTypes', 'statistics', 'foreignAlbumId')})
        return slim
    return albums_cache.get(load)


def album_state(a, jobs_by_album):
    st = a.get('statistics') or {}
    have, total = st.get('trackFileCount', 0), st.get('trackCount', 0)
    j = jobs_by_album.get(a['id'])
    released = (a.get('releaseDate') or '9999')[:10] <= time.strftime('%Y-%m-%d')
    if j and j['status'] in mf.ACTIVE:
        status = 'active'
    elif total and have >= total:
        status = 'have'
    elif have:
        status = 'partial'
    elif not released:
        status = 'unreleased'
    else:
        status = 'missing'
    return status, have, total


def jobs_by_album():
    out = {}
    for j in sorted(mf.JOBS.all(), key=lambda j: j.get('created', 0)):
        if j.get('album_id'):
            out[j['album_id']] = j
        for aid in j.get('album_ids') or []:
            out[aid] = j
    return out


# ---------------------------------------------------------------- searches

class Searches:
    """Searches run a few at a time (mf.SEARCH_WORKERS); slskd only needs their starts spaced out.

    Each album's slskd search is kept (and its id saved in state/searches.json) until the album
    is in the library, so after a failed download you can pick another source straight away,
    even after this app restarts."""

    def __init__(self):
        self.results = {}
        self.q = queue.Queue()
        for _ in range(mf.SEARCH_WORKERS):
            threading.Thread(target=self.worker, daemon=True).start()

    def request(self, album_id, text=None):
        cur = self.results.get(album_id)
        if cur and cur['state'] in ('queued', 'searching', 'loading'):
            return
        self.results[album_id] = {'state': 'queued', 'text': text, 'at': time.time()}
        self.q.put(('search', album_id, text))

    def worker(self):
        while True:
            kind, album_id, text = self.q.get()
            r = self.results[album_id]
            try:
                album = lid.album(album_id)
                rel = mf.choose_release(album)
                if kind == 'restore':
                    saved = mf.SEARCHES.get(album_id) or {}
                    responses = slsk.responses(saved.get('sid'))
                    if responses is None:  # slskd no longer has it
                        mf.SEARCHES.remove(album_id, {'sid': saved.get('sid')})
                        self.results.pop(album_id, None)
                        continue
                    cands = mf.rank(responses, lid, album, rel, EXTS)
                    r.update(state='done', text=saved.get('text'), candidates=cands, sid=saved.get('sid'),
                             release=mf.describe_release(rel), at=saved.get('at') or time.time())
                    continue
                r['state'] = 'searching'
                artist = next(a for a in all_artists() if a['id'] == album['artistId'])
                text, cands, rel, sid = mf.search_album(slsk, lid, artist['artistName'], album, EXTS, text)
                slsk.delete_search(mf.remember_search(album_id, sid, text))
                r.update(state='done', text=text, candidates=cands, sid=sid,
                         release=mf.describe_release(rel), at=time.time())
            except Exception as e:
                traceback.print_exc()
                r.update(state='error', error=str(e), at=time.time())

    def reformat(self, wanted):
        """Re-rank every result already on screen for a new wanted format."""
        for r in list(self.results.values()):
            if r.get('candidates'):
                mf.apply_format(r['candidates'], wanted)

    def public(self, album_id, failed=()):
        r = self.results.get(album_id)
        if not r and mf.SEARCHES.get(album_id):
            # Results from before a restart: load them back from slskd.
            r = self.results[album_id] = {'state': 'loading', 'at': time.time()}
            self.q.put(('restore', album_id, None))
        if not r:
            return None
        out = {k: r.get(k) for k in ('state', 'text', 'release', 'error', 'at')}
        if r.get('candidates') is not None:
            out['candidates'] = [candidate_json(c, failed) for c in r['candidates'][:25]]
            out['total'] = len(r['candidates'])
        return out


def candidate_json(c, failed=()):
    f0 = c['files'][0]
    q = mf.quality_label(c)
    return {'format_ok': c.get('format_ok'), 'score': round(c['score'] * 100), 'username': c['username'], 'dir': c['dir'],
            'folder': '\\'.join(c['dir'].split('\\')[-2:]), 'count': len(c['files']),
            'ext': os.path.splitext(f0['filename'])[1].lstrip('.').lower(), 'quality': q,
            'size': c.get('size', 0), 'free': c['free'], 'speed': c['speed'], 'queue': c['queue'],
            'multidisc': c['multidisc'], 'failed': (c['username'], c['dir']) in failed,
            'files': [f['filename'].replace('/', '\\').split('\\')[-1] for f in c['files']][:60]}


searches = Searches() if slsk else None


# ---------------------------------------------------------------- queue worker

worker_state = {'running': False, 'note': 'starting'}


def worker():
    lock = None
    runner = None
    while True:
        try:
            if lock is None:
                lock = mf.acquire_runner_lock()
                if lock is None:
                    worker_state.update(running=False, note='a terminal musicflow run is handling downloads')
                    time.sleep(15)
                    continue
                runner = mf.Runner(cfg)
                worker_state.update(running=True, note='')
            before = runner.finished
            runner.step()
            if runner.finished != before:
                albums_cache.clear()
            time.sleep(5)
        except Exception:
            traceback.print_exc()
            time.sleep(15)


# ---------------------------------------------------------------- API

def job_json(j):
    out = {k: v for k, v in j.items() if k not in ('files',)}
    out['file_count'] = len(j.get('files') or [])
    out['size'] = sum(f.get('size') or 0 for f in j.get('files') or [])
    return out


def api_overview():
    jobs = jobs_by_album()
    stats = {}
    for a in all_albums():
        if not a['monitored']:
            continue
        s = stats.setdefault(a['artistId'], {'total': 0, 'have': 0, 'missing': 0, 'partial': 0, 'active': 0})
        status, _, _ = album_state(a, jobs)
        if status == 'unreleased':
            continue
        s['total'] += 1
        s[{'have': 'have', 'partial': 'partial', 'missing': 'missing', 'active': 'active'}[status]] += 1
    out = []
    for ar in all_artists():
        s = stats.get(ar['id'], {'total': 0, 'have': 0, 'missing': 0, 'partial': 0, 'active': 0})
        out.append({'id': ar['id'], 'name': ar['artistName'], 'monitored': ar.get('monitored'), **s})
    out.sort(key=lambda a: a['name'].lower().removeprefix('the '))
    active = [j for j in mf.JOBS.all() if j['status'] in mf.ACTIVE]
    return {'artists': out, 'active_jobs': len(active), 'worker': worker_state, **api_settings(),
            'jellyfin': bool(cfg.get('jellyfin_key')), 'slskd': bool(slsk)}


def api_artist(artist_id):
    artist = lid.req('GET', f'artist/{artist_id}')
    albums = lid.albums(artist_id)
    jobs = jobs_by_album()
    notes = mf.ALBUM_NOTES.all()
    failed_sources = {}
    for j in mf.JOBS.all():
        if j['kind'] == 'download' and j['status'] == 'failed':
            failed_sources.setdefault(j['album_id'], set()).add((j['username'], j['dir']))
    out = []
    for a in sorted(albums, key=lambda a: a.get('releaseDate') or ''):
        status, have, total = album_state(a, jobs)
        rel = mf.choose_release(a)
        sel = next((r for r in a.get('releases') or [] if r.get('monitored')), None)
        j = jobs.get(a['id'])
        out.append({
            'id': a['id'], 'title': a['title'], 'year': (a.get('releaseDate') or '')[:4],
            'type': a.get('albumType'), 'secondary': [s.get('name', s) if isinstance(s, dict) else s
                                                      for s in a.get('secondaryTypes') or []],
            'monitored': a['monitored'], 'status': status, 'have': have, 'total': total,
            'want': mf.describe_release(rel) if rel else None,
            'selected': mf.describe_release(sel) if sel else None,
            'job': job_json(j) if j else None,
            'search': searches.public(a['id'], failed_sources.get(a['id'], set())) if searches else None,
            'date': notes.get(str(a['id']), {}).get('date'),
            'date_src': notes.get(str(a['id']), {}).get('date_src'),
            'date_override': notes.get(str(a['id']), {}).get('override'),
        })
    return {'id': artist['id'], 'name': artist['artistName'], 'path': mf.host_path(artist['path']),
            'albums': out}


def api_pick(body):
    jobs = []
    busy = {j.get('album_id') for j in mf.JOBS.all() if j['status'] in mf.ACTIVE}
    for p in body.get('picks') or []:
        aid = int(p['album_id'])
        if aid in busy:
            continue
        r = searches.results.get(aid) if searches else None
        cands = (r or {}).get('candidates') or []
        # Picks name the source (results can be re-sorted when the wanted format changes).
        cand = next((c for c in cands if c['username'] == p.get('username') and c['dir'] == p.get('dir')), None)
        if not r or r.get('state') != 'done' or not cand:
            raise ValueError(f'no search result to pick for album {aid}; search again')
        album = lid.album(aid)
        artist = lid.req('GET', f'artist/{album["artistId"]}')
        jobs.append(mf.download_job(artist, album, cand, search_id=r.get('sid')))
    if jobs:
        mf.JOBS.add(*jobs)
    return {'added': len(jobs)}


def api_settings():
    return {'format': mf.wanted_format(), 'formats': mf.FORMATS}


def api_set_settings(body):
    if 'format' in body:
        mf.set_wanted_format(body['format'])
        if searches:
            searches.reformat(body['format'])
    return api_settings()


def api_date(body):
    """Pin (or un-pin) an album's date, then re-tag it."""
    album = lid.album(int(body['album_id']))
    date = (body.get('date') or '').strip() or None
    mf.set_date_override(album['id'], date)
    artist = lid.req('GET', f'artist/{album["artistId"]}')
    j = mf.new_job('finish', artist_id=artist['id'], artist=artist['artistName'], album_ids=[album['id']],
                   album=album['title'])
    mf.JOBS.add(j)
    return {'job': j['id']}


def api_finish(body):
    artist = lid.req('GET', f'artist/{int(body["artist_id"])}')
    ids = [int(x) for x in body.get('album_ids') or []]
    if not ids:
        ids = [a['id'] for a in lid.albums(artist['id']) if (a.get('statistics') or {}).get('trackFileCount')]
    titles = [lid.album(i)['title'] for i in ids] if len(ids) <= 1 else []
    j = mf.new_job('finish', artist_id=artist['id'], artist=artist['artistName'], album_ids=ids,
                   album=titles[0] if titles else f'{len(ids)} albums', force_cover=bool(body.get('force_cover')))
    mf.JOBS.add(j)
    return {'job': j['id']}


def safe_folder(path):
    p = os.path.realpath(mf.container_path(path))
    if not p.startswith(mf.DOWNLOADS + '/') or p.startswith(mf.STAGING) or p.startswith(mf.DOWNLOADS + '/incomplete'):
        raise ValueError('folder must be inside the slskd downloads folder')
    if not os.path.isdir(p):
        raise ValueError('folder not found')
    return p


def api_disk():
    """Size and free space of the drive(s) holding the library and the downloads."""
    seen, out = set(), []
    for label, path in (('Music', mf.MUSIC), ('Downloads', mf.DOWNLOADS)):
        st = os.statvfs(path)
        dev = os.stat(path).st_dev
        if dev in seen:
            continue
        seen.add(dev)
        total, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
        out.append({'label': label, 'total': total, 'free': free})
    return {'drives': out}


def api_folders():
    out = []
    skip = {'incomplete', os.path.basename(mf.STAGING)}

    def visit(d, depth):
        try:
            names = sorted(os.listdir(d), key=str.lower)
        except OSError:
            return
        audio = [n for n in names if os.path.splitext(n)[1].lower() in mf.LOSSLESS | mf.LOSSY and not n.startswith('._')]
        if audio:
            out.append({'path': mf.host_path(d), 'name': os.path.relpath(d, mf.DOWNLOADS), 'files': len(audio),
                        'mtime': os.path.getmtime(d)})
        if depth < 3:
            for n in names:
                p = os.path.join(d, n)
                if n not in skip and not n.startswith('.') and os.path.isdir(p):
                    visit(p, depth + 1)
    visit(mf.DOWNLOADS, 0)
    out.sort(key=lambda f: -f['mtime'])
    return {'folders': out}


def api_import_guess(body):
    folder = safe_folder(body['folder'])
    artist = lid.req('GET', f'artist/{int(body["artist_id"])}')
    guess = mf.guess_album(lid, artist, folder)
    albums = [{'id': a['id'], 'title': a['title'], 'year': (a.get('releaseDate') or '')[:4]}
              for a in sorted(lid.albums(artist['id']), key=lambda a: a.get('releaseDate') or '')]
    return {'guess': guess['id'], 'albums': albums,
            'files': len(mf.audio_files(folder, mf.LOSSLESS | mf.LOSSY))}


def api_import(body):
    folder = safe_folder(body['folder'])
    artist = lid.req('GET', f'artist/{int(body["artist_id"])}')
    album = lid.album(int(body['album_id']))
    j = mf.new_job('import', artist_id=artist['id'], artist=artist['artistName'], album_id=album['id'],
                   album=album['title'], folder=folder, force_cover=bool(body.get('force_cover')),
                   allow_lossy=bool(body.get('allow_lossy')))
    mf.JOBS.add(j)
    return {'job': j['id']}


def api_job_action(jid, action):
    j = mf.JOBS.get(jid)
    if not j:
        raise ValueError('job not found')
    if action == 'dismiss':
        if j['status'] in ('queued', 'processing'):
            raise ValueError('job is running')
        mf.JOBS.remove(jid)
    elif action == 'retry':
        if j['status'] != 'failed':
            raise ValueError('only failed jobs can be retried')
        status = 'picked' if j['kind'] == 'download' else 'pending'
        if j['kind'] == 'download' and all(mf.find_downloaded(f['filename'], f['size']) for f in j['files']):
            status = 'downloaded'  # files are here; only the import failed
        mf.JOBS.update(jid, status=status, error=None, log=(j.get('log') or []) + ['--- retry ---'])
    elif action == 'cancel':
        if j['status'] not in ('picked', 'queued', 'pending'):
            raise ValueError('job cannot be cancelled now')
        if j['status'] == 'queued' and slsk:
            tr = slsk.transfers(j['username'])
            slsk.clear(j['username'], [tr.get(f['filename']) for f in j['files']])
        mf.JOBS.update(jid, status='failed', error='cancelled')
    return {'ok': True}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = 'musicflow'

    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype='application/json'):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'private, max-age=3600' if ctype.startswith('image/') else 'no-store')
        for k, v in formlogin.SECURITY_HEADERS:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return json.loads(self.rfile.read(n) or b'{}') if n else {}

    def do_GET(self):
        self.route('GET')

    def do_POST(self):
        self.route('POST')

    def route(self, method):
        path = urllib.parse.urlparse(self.path).path
        path = re.sub(r'^/musicflow(?=/|$)', '', path) or '/'
        if formlogin.httpserver.handle(self, LOGIN, path):
            return
        try:
            if method == 'GET' and path in ('/', '/index.html'):
                with open(os.path.join(STATIC, 'index.html'), 'rb') as f:
                    return self.send(200, f.read(), 'text/html; charset=utf-8')
            if method == 'GET' and path == '/api/overview':
                return self.send(200, api_overview())
            m = re.fullmatch(r'/api/artist/(\d+)', path)
            if method == 'GET' and m:
                return self.send(200, api_artist(int(m.group(1))))
            m = re.fullmatch(r'/api/art/album/(\d+)', path)
            if method == 'GET' and m:
                try:
                    data = mf.http('GET', f'{lid.url}/api/v1/mediacover/album/{m.group(1)}/cover-250.jpg',
                                   {'X-Api-Key': lid.key}, raw=True)
                    return self.send(200, data, 'image/jpeg')
                except mf.HttpError:
                    return self.send(404, b'', 'image/jpeg')
            if method == 'GET' and path == '/api/jobs':
                jobs = sorted(mf.JOBS.all(), key=lambda j: -j.get('created', 0))
                return self.send(200, {'jobs': [job_json(j) for j in jobs], 'worker': worker_state})
            if method == 'GET' and path == '/api/settings':
                return self.send(200, {**api_settings(), 'auth': LOGIN.enabled})
            if method == 'POST' and path == '/api/settings':
                return self.send(200, api_set_settings(self.body()))
            if method == 'GET' and path == '/api/disk':
                return self.send(200, api_disk())
            if method == 'GET' and path == '/api/folders':
                return self.send(200, api_folders())
            if method == 'POST' and path == '/api/search':
                if not searches:
                    raise ValueError('no slskd API key configured')
                b = self.body()
                for aid in b.get('album_ids') or []:
                    searches.request(int(aid), (b.get('text') or '').strip() or None)
                return self.send(200, {'ok': True})
            if method == 'POST' and path == '/api/pick':
                return self.send(200, api_pick(self.body()))
            if method == 'POST' and path == '/api/date':
                return self.send(200, api_date(self.body()))
            if method == 'POST' and path == '/api/finish':
                return self.send(200, api_finish(self.body()))
            if method == 'POST' and path == '/api/import/guess':
                return self.send(200, api_import_guess(self.body()))
            if method == 'POST' and path == '/api/import':
                return self.send(200, api_import(self.body()))
            if method == 'POST' and path == '/api/jobs/clear':
                mf.JOBS.remove_finished()
                return self.send(200, {'ok': True})
            m = re.fullmatch(r'/api/jobs/([\w-]+)/(retry|dismiss|cancel)', path)
            if method == 'POST' and m:
                return self.send(200, api_job_action(m.group(1), m.group(2)))
            if method == 'POST' and path == '/api/jellyfin':
                mf.jellyfin_refresh(cfg)
                return self.send(200, {'ok': True})
            if method == 'POST' and path == '/api/refresh':
                artists_cache.clear()
                albums_cache.clear()
                return self.send(200, {'ok': True})
            self.send(404, {'error': 'not found'})
        except (ValueError, KeyError) as e:
            self.send(400, {'error': str(e)})
        except Exception as e:
            traceback.print_exc()
            self.send(500, {'error': str(e)})


def main():
    os.umask(0o002)
    threading.Thread(target=worker, daemon=True).start()
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    srv.daemon_threads = True
    print(f'musicflow web on :{PORT}', flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()

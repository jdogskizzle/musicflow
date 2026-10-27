#!/usr/bin/env python3
"""musicflow: fill in an artist's missing Lidarr albums from Soulseek, then finish them.

The steps this replaces (music-download-list.txt):
  1. pick and download each album on slskd      -> search + ranked pick you confirm
  2-4. move, rescan and rename in Lidarr        -> Lidarr manual import onto the
                                                   normal CD release (fewest discs)
  5-6. lyrics from LRCGET                       -> same LRCLIB lookup, writes .lrc/.txt
  7-8. iTunes artwork finder -> cover.jpg       -> same iTunes source, full-size image
  9. Picard tags + Wikipedia release date       -> MusicBrainz tags, date from Wikipedia
  10. Jellyfin music library scan               -> API refresh
  plus the group read/write permission fix.

Commands:
  get ARTIST       search, pick, download and finish every missing album
  resume           continue downloads from an earlier `get` that was stopped
  import ARTIST DIR   import a folder you downloaded by hand, then finish it
  finish ARTIST    redo cover, tags, lyrics and permissions for albums already imported
"""
import argparse
import fcntl
import io
import json
import math
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher

import mutagen
import yaml
from mutagen.flac import FLAC, Picture
from PIL import Image

# Library and downloads, at the same paths Lidarr sees them (set in .env).
MUSIC = os.environ.get('MUSIC_PATH', '/music').rstrip('/')
DOWNLOADS = os.environ.get('DOWNLOADS_PATH', '/downloads').rstrip('/')
STAGING = os.path.join(DOWNLOADS, 'musicflow-staging')
STATE = '/state'
JOBS_FILE = os.path.join(STATE, 'jobs.json')
# The same folders as you see them on the host, so paths you type or see match your machine.
HOST_PATHS = {(os.environ.get('HOST_DOWNLOADS_DIR') or DOWNLOADS).rstrip('/'): DOWNLOADS,
              (os.environ.get('HOST_MUSIC_DIR') or MUSIC).rstrip('/'): MUSIC}

LOSSLESS = {'.flac'}
LOSSY = {'.mp3', '.m4a', '.ogg', '.opus'}
KEEP_TAGS = re.compile(r'^(replaygain_.*|lyrics|unsyncedlyrics)$', re.I)
DISC_DIR = re.compile(r'^(cd|disc|disk|dvd|vol(ume)?)[\s._-]*\d+\b', re.I)

contact = os.environ.get('MB_CONTACT') or 'personal library tool'
UA = f'musicflow/1.0 ( {contact} )'


# ---------------------------------------------------------------- output

class C:
    on = sys.stdout.isatty()
    @staticmethod
    def w(code, s): return f'\033[{code}m{s}\033[0m' if C.on else s
def bold(s): return C.w('1', s)
def dim(s): return C.w('2', s)
def green(s): return C.w('32', s)
def yellow(s): return C.w('33', s)
def red(s): return C.w('31', s)

_tls = threading.local()
ANSI = re.compile(r'\033\[[0-9;]*m')


def log(msg=''):
    print(msg, flush=True)
    sink = getattr(_tls, 'sink', None)
    if sink is not None:
        sink.append(ANSI.sub('', str(msg)))


def warn(msg): log(yellow('  ! ' + msg))


# ---------------------------------------------------------------- http

class HttpError(Exception):
    def __init__(self, code, body, url):
        super().__init__(f'HTTP {code} for {url}: {body[:200]!r}')
        self.code = code


def http(method, url, headers=None, body=None, timeout=60, raw=False):
    h = {'User-Agent': UA, 'Accept': 'application/json'}
    h.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            b = r.read()
    except urllib.error.HTTPError as e:
        raise HttpError(e.code, e.read() or b'', url) from None
    if raw:
        return b
    return json.loads(b) if b.strip() else None


_last_call = {}

def ext_get(url, host, gap=0.0, raw=False, tries=4):
    """GET from a public service, spacing calls per host and retrying busy errors."""
    for attempt in range(tries):
        wait = gap - (time.time() - _last_call.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        _last_call[host] = time.time()
        try:
            return http('GET', url, raw=raw)
        except HttpError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt < tries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise


# ---------------------------------------------------------------- text matching

def norm(s):
    s = unicodedata.normalize('NFKD', s or '')
    s = ''.join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = s.replace('&', ' and ').replace("'", '').replace('’', '')
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    return ' '.join(s.split())


def sim(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def title_in(title, text):
    """How well a track title is found inside a filename or tag."""
    t, x = norm(title), norm(text)
    if not t or not x:
        return 0.0
    if t == x:
        return 1.0
    if re.search(r'(^| )' + re.escape(t) + r'( |$)', x):
        return 0.95 if len(t) >= 3 else 0.7
    return SequenceMatcher(None, t, x).ratio()


def clean_stem(name):
    stem = os.path.splitext(name)[0]
    s = norm(stem)
    return re.sub(r'^(\d+ )+', '', s)


# ---------------------------------------------------------------- config

def load_config():
    cfg = {
        'lidarr_url': os.environ.get('LIDARR_URL', 'http://lidarr:8686').rstrip('/'),
        'slskd_url': os.environ.get('SLSKD_URL', 'http://slskd:5030').rstrip('/'),
        'jellyfin_url': os.environ.get('JELLYFIN_URL', 'http://jellyfin:8096').rstrip('/'),
        'lidarr_key': os.environ.get('LIDARR_API_KEY'),
        'slskd_key': os.environ.get('SLSKD_API_KEY'),
        'jellyfin_key': os.environ.get('JELLYFIN_API_KEY') or None,
        'itunes_country': os.environ.get('ITUNES_COUNTRY') or 'us',
    }
    if not cfg['lidarr_key'] and os.path.isfile('/secrets/lidarr-config.xml'):
        cfg['lidarr_key'] = ET.parse('/secrets/lidarr-config.xml').getroot().findtext('ApiKey')
    if not cfg['slskd_key'] and os.path.isfile('/secrets/slskd.yml'):
        with open('/secrets/slskd.yml') as f:
            y = yaml.safe_load(f) or {}
        keys = (((y.get('web') or {}).get('authentication') or {}).get('api_keys') or {})
        for v in keys.values():
            if isinstance(v, dict) and v.get('key'):
                cfg['slskd_key'] = v['key']
                break
    if not cfg['lidarr_key']:
        sys.exit('No Lidarr API key found: set LIDARR_API_KEY or LIDARR_CONFIG in .env.')
    return cfg


# ---------------------------------------------------------------- Lidarr

class Lidarr:
    def __init__(self, url, key):
        self.url, self.key = url, key

    def req(self, method, path, params=None, body=None, timeout=120):
        u = f'{self.url}/api/v1/{path}'
        if params:
            u += '?' + urllib.parse.urlencode(params)
        return http(method, u, {'X-Api-Key': self.key}, body, timeout=timeout)

    def find_artist(self, name):
        artists = self.req('GET', 'artist')
        exact = [a for a in artists if norm(a['artistName']) == norm(name)]
        if len(exact) == 1:
            return exact[0]
        close = sorted(artists, key=lambda a: -sim(a['artistName'], name))
        close = [a for a in close if sim(a['artistName'], name) > 0.75 or norm(name) in norm(a['artistName'])]
        if len(exact) > 1 or len(close) != 1:
            opts = ', '.join(a['artistName'] for a in (exact or close)[:8]) or 'none'
            sys.exit(f'Could not pick one Lidarr artist for "{name}". Close matches: {opts}')
        return close[0]

    def albums(self, artist_id):
        return self.req('GET', 'album', {'artistId': artist_id})

    def album(self, album_id):
        return self.req('GET', f'album/{album_id}')

    def release_tracks(self, release_id):
        return self.req('GET', 'track', {'albumReleaseId': release_id})

    def album_tracks(self, album_id):
        return self.req('GET', 'track', {'albumId': album_id})

    def trackfiles(self, album_id):
        return self.req('GET', 'trackfile', {'albumId': album_id})

    def run_command(self, body, timeout=1800):
        c = self.req('POST', 'command', body=body)
        start = time.time()
        while c.get('status') not in ('completed', 'failed', 'aborted', 'cancelled'):
            if time.time() - start > timeout:
                raise RuntimeError(f'Lidarr command {body["name"]} did not finish in time')
            time.sleep(2)
            c = self.req('GET', f'command/{c["id"]}')
        if c['status'] != 'completed':
            raise RuntimeError(f'Lidarr command {body["name"]} {c["status"]}: {c.get("message") or c.get("exception") or ""}')
        return c

    def select_release(self, album, release_id):
        """Make release_id the album's selected (monitored) release."""
        a = self.album(album['id'])
        if all(r['monitored'] == (r['id'] == release_id) for r in a['releases']):
            return a
        for r in a['releases']:
            r['monitored'] = r['id'] == release_id
        self.req('PUT', f'album/{a["id"]}', body=a)
        return self.album(album['id'])


# ---------------------------------------------------------------- release choice

def release_tier(fmt):
    """0 = normal CD, 1 = other CD flavours, 2 = digital, 3 = anything else, 4 = vinyl/cassette."""
    f = (fmt or '').strip()
    if re.fullmatch(r'(\d+x)?CD', f):
        return 0
    if re.search(r'vinyl|cassette|"', f, re.I):
        return 4
    if 'CD' in f and not re.search(r'DVD|Blu-ray|SACD', f, re.I):
        return 1
    if f.lower().startswith('digital'):
        return 2
    return 3


def country_rank(r):
    c = r.get('country') or []
    if 'United States' in c:
        return 0
    if '[Worldwide]' in c:
        return 1
    return 2


def choose_release(album, track_count=None):
    """Normal CD release with the fewest discs; optionally only those with track_count tracks."""
    rels = album.get('releases') or []
    if track_count is not None:
        rels = [r for r in rels if r.get('trackCount') == track_count]
    if not rels:
        return None
    cd_counts = Counter(r['trackCount'] for r in album['releases'] if release_tier(r.get('format')) == 0)

    def key(r):
        return (release_tier(r.get('format')), r.get('mediumCount') or 99,
                0 if (r.get('status') or '').lower() == 'official' else 1,
                0 if r.get('monitored') else 1, -cd_counts.get(r['trackCount'], 0),
                country_rank(r), r['id'])
    return min(rels, key=key)


def describe_release(r):
    discs = r.get('mediumCount') or 1
    return f'{r.get("format") or "?"}, {discs} disc{"s" if discs != 1 else ""}, {r.get("trackCount")} tracks'


# ---------------------------------------------------------------- slskd

class Slskd:
    # slskd refuses two search *starts* at the same moment (HTTP 429), but runs many searches
    # side by side once started. So starts go one at a time, a little apart.
    _start_lock = threading.Lock()
    _last_start = 0.0
    START_GAP = 1.0

    def __init__(self, url, key):
        self.url, self.key = url, key

    def req(self, method, path, body=None, timeout=60):
        return http(method, f'{self.url}/api/v0/{path}', {'X-Api-Key': self.key}, body, timeout=timeout)

    def search(self, text, max_wait=60):
        """Run a search. Returns (search id, responses). The search stays in slskd so its
        results can be reused if a download fails; delete it with delete_search()."""
        sid = str(uuid.uuid4())
        with Slskd._start_lock:
            for attempt in range(20):
                wait = Slskd.START_GAP - (time.time() - Slskd._last_start)
                if wait > 0:
                    time.sleep(wait)
                try:
                    self.req('POST', 'searches', {'id': sid, 'searchText': text, 'searchTimeout': 15000,
                                                  'responseLimit': 500, 'fileLimit': 20000, 'filterResponses': True})
                    break
                except HttpError as e:
                    if e.code != 429 or attempt == 19:
                        raise
                    time.sleep(2)
                finally:
                    Slskd._last_start = time.time()
        try:
            return sid, self._collect(sid, max_wait)
        except Exception:
            self.delete_search(sid)
            raise

    def responses(self, sid):
        """Results of an earlier search, or None if slskd no longer has it."""
        try:
            return self.req('GET', f'searches/{sid}/responses') or []
        except HttpError as e:
            if e.code == 404:
                return None
            raise

    def delete_search(self, sid):
        if not sid:
            return
        try:
            self.req('DELETE', f'searches/{sid}')
        except (HttpError, urllib.error.URLError):
            pass

    def _collect(self, sid, max_wait):
        start = time.time()
        while True:
            time.sleep(3)
            s = self.req('GET', f'searches/{sid}')
            if s.get('isComplete'):
                break
            if time.time() - start > max_wait:
                try:
                    self.req('PUT', f'searches/{sid}')
                except HttpError:
                    pass
                time.sleep(1)
                break
        return self.req('GET', f'searches/{sid}/responses') or []

    def enqueue(self, username, files):
        self.req('POST', f'transfers/downloads/{urllib.parse.quote(username)}',
                 [{'filename': f['filename'], 'size': f['size']} for f in files])

    def transfers(self, username):
        try:
            d = self.req('GET', f'transfers/downloads/{urllib.parse.quote(username)}')
        except HttpError as e:
            if e.code == 404:
                return {}
            raise
        out = {}
        for directory in (d or {}).get('directories') or []:
            for f in directory.get('files') or []:
                out[f['filename']] = f
        return out

    def cancel(self, username, transfer_id, remove=False):
        q = '?remove=true' if remove else ''
        try:
            self.req('DELETE', f'transfers/downloads/{urllib.parse.quote(username)}/{transfer_id}{q}')
        except (HttpError, urllib.error.URLError):
            pass

    def clear(self, username, transfers):
        """Take a finished job's transfers off slskd's Downloads list (files on disk stay).
        Unfinished ones are cancelled first, since slskd only removes completed transfers."""
        transfers = [t for t in transfers if t]
        running = [t for t in transfers if not t['state'].startswith('Completed')]
        for t in running:
            self.cancel(username, t['id'])
        if running:
            time.sleep(2)
        for t in transfers:
            self.cancel(username, t['id'], remove=True)


def search_text(artist, album):
    # Soulseek matches words; punctuation tends to hide results. It splits "England's" into
    # "england" + "s", so an apostrophe has to split the word here too ("englands" finds nothing).
    words = norm(re.sub(r"['’‘`´]", ' ', f'{artist} {album}')).split()
    return ' '.join(w for w in words if len(w) > 1 or w.isdigit()) or f'{artist} {album}'


def build_candidates(responses, exts):
    cands = []
    for resp in responses:
        groups = defaultdict(list)
        discs = {}
        for f in resp.get('files') or []:
            parts = f['filename'].replace('/', '\\').split('\\')
            if os.path.splitext(parts[-1])[1].lower() not in exts:
                continue
            dirs = parts[:-1]
            if dirs and DISC_DIR.match(dirs[-1]) and len(dirs) > 1:
                key = '\\'.join(dirs[:-1])
                discs[key] = True
            else:
                key = '\\'.join(dirs)
            groups[key].append(f)
        for key, files in groups.items():
            files.sort(key=lambda f: f['filename'])
            cands.append({'username': resp['username'], 'dir': key, 'files': files,
                          'multidisc': key in discs,
                          'free': bool(resp.get('hasFreeUploadSlot')),
                          'speed': resp.get('uploadSpeed') or 0,
                          'queue': resp.get('queueLength') or 0})
    return cands


def score_candidate(c, target_tracks, alt_counts, album_title):
    files = c['files']
    n, m = len(files), len(target_tracks)
    if n == m:
        count_s = 1.0
    elif n in alt_counts:
        count_s = 0.7
    else:
        count_s = max(0.0, 1 - abs(n - m) / max(m, 1) * 2) * 0.4

    stems = [clean_stem(f['filename'].replace('/', '\\').split('\\')[-1]) for f in files]
    used, sims, dur_ok, dur_n = set(), [], 0, 0
    for t in target_tracks:
        best, best_i = 0.0, None
        for i, s in enumerate(stems):
            if i in used:
                continue
            v = title_in(t['title'], s)
            if v > best:
                best, best_i = v, i
        sims.append(best)
        if best_i is not None and best >= 0.6:
            used.add(best_i)
            length = files[best_i].get('length')
            if length and t.get('duration'):
                dur_n += 1
                if abs(length - t['duration'] / 1000) <= 3:
                    dur_ok += 1
    title_s = sum(sims) / len(sims) if sims else 0
    dur_s = dur_ok / dur_n if dur_n else 0.5
    folder_s = 1.0 if norm(album_title) and norm(album_title) in norm(c['dir']) else sim(album_title, c['dir'].split('\\')[-1])
    speed_s = min(1.0, math.log10(1 + c['speed'] / 50_000) / math.log10(41)) if c['speed'] else 0
    avail = 0.45 * c['free'] + 0.35 * speed_s + 0.2 * (1.0 if c['queue'] == 0 else max(0.0, 1 - c['queue'] / 50))
    c['base'] = 0.40 * title_s + 0.25 * count_s + 0.15 * dur_s + 0.05 * folder_s + 0.15 * avail
    c['score'] = c['base']
    c['size'] = sum(f.get('size') or 0 for f in files)
    return c


# ---------------------------------------------------------------- wanted format

FORMATS = {'cd': 'CD 16/44.1', 'hires': 'Hi-res 24-bit', 'any': 'Any lossless'}
HIRES_KBPS = 1450  # a 16/44.1 stereo FLAC can't go much past 1411 kbps


def candidate_format(c):
    """(bits, rate, kbps) for a candidate's FLAC files. bits/rate come from what the source
    reports (the most common pair); kbps is the average bitrate, used when they report nothing."""
    flac = [f for f in c['files'] if f['filename'].lower().endswith('.flac')]
    if not flac:
        return None, None, None
    pairs = Counter((f.get('bitDepth'), f.get('sampleRate')) for f in flac if f.get('bitDepth') and f.get('sampleRate'))
    bits, rate = pairs.most_common(1)[0][0] if pairs else (None, None)
    timed = [f for f in flac if f.get('length') and f.get('size')]
    secs = sum(f['length'] for f in timed)
    kbps = sum(f['size'] for f in timed) * 8 / secs / 1000 if secs else None
    return bits, rate, kbps


def format_penalty(c, wanted):
    """How far a candidate is from the wanted format: 0 = match. Also sets c['format_ok']."""
    bits, rate, kbps = candidate_format(c)
    lossless = any(f['filename'].lower().endswith('.flac') for f in c['files'])
    if wanted == 'any':
        c['format_ok'] = None if lossless else False
        return 0.0
    if not lossless:
        c['format_ok'] = False
        return 0.3
    hires = None  # True / False / None (can't tell)
    if bits:
        hires = bits > 16 or rate > 48000
    elif kbps and kbps > HIRES_KBPS:
        hires = True
    if wanted == 'cd':
        if bits == 16 and rate == 44100:
            c['format_ok'] = True
            return 0.0
        if hires is None:
            c['format_ok'] = None
            return 0.05
        c['format_ok'] = False
        if bits == 16:          # 16/48: close
            return 0.15
        if rate and rate <= 48000:
            return 0.2          # 24/44.1, 24/48
        return 0.3
    # hires
    if hires:
        c['format_ok'] = True
        return 0.0
    if hires is None:
        c['format_ok'] = None
        return 0.05
    c['format_ok'] = False
    return 0.25


def apply_format(cands, wanted):
    """Re-score and re-sort ranked candidates for a wanted format (in place)."""
    for c in cands:
        c['score'] = max(0.0, c['base'] - format_penalty(c, wanted))
    cands.sort(key=lambda c: -c['score'])
    return cands


def quality_label(c):
    bits, rate, kbps = candidate_format(c)
    if bits and rate:
        return f'{bits}/{rate / 1000:g}'
    f0 = c['files'][0]
    if f0.get('bitRate'):
        return f'{f0["bitRate"]} kbps'
    if kbps:
        return f'~{kbps:.0f} kbps' + (' (hi-res)' if kbps > HIRES_KBPS else '')
    return ''


def describe_candidate(i, c):
    f0 = c['files'][0]
    ext = os.path.splitext(f0['filename'])[1].lstrip('.').lower()
    q = quality_label(c)
    q = red(q) if c.get('format_ok') is False else q
    slot = green('free slot') if c['free'] else yellow(f'queue {c["queue"]}')
    speed = f'{c["speed"] / 1e6:.1f} MB/s'
    folder = '\\'.join(c['dir'].split('\\')[-2:])
    disc = ' [discs]' if c['multidisc'] else ''
    return (f'  {bold(str(i))}) {c["score"] * 100:3.0f}%  {c["username"][:18]:<18} {len(c["files"]):>2} {ext} {q}'
            f'{" " * max(0, 8 - len(ANSI.sub("", q)))} {c["size"] / 1e6:6.0f} MB  {slot:<9} {speed:>9}  {dim(folder)}{disc}')


def local_path(remote):
    """Where slskd saves a remote file: downloads/<remote parent folder>/<file name>."""
    parts = remote.replace('/', '\\').split('\\')
    return os.path.join(DOWNLOADS, parts[-2] if len(parts) > 1 else '', parts[-1])


def find_downloaded(remote, size):
    p = local_path(remote)
    if os.path.exists(p) and os.path.getsize(p) == size:
        return p
    # slskd adds a suffix when the name was already taken.
    d, name = os.path.split(p)
    stem, ext = os.path.splitext(name)
    if os.path.isdir(d):
        hits = [os.path.join(d, x) for x in os.listdir(d)
                if x.startswith(stem + '_') and x.endswith(ext) and os.path.getsize(os.path.join(d, x)) == size]
        if hits:
            return max(hits, key=os.path.getmtime)
    return None


# ---------------------------------------------------------------- files

def audio_files(folder, exts):
    out = []
    for root, _, names in os.walk(folder):
        for n in names:
            if not n.startswith('._') and os.path.splitext(n)[1].lower() in exts:
                out.append(os.path.join(root, n))
    return out


def file_info(path):
    """(disc, track, title, seconds) from tags, falling back to the file name."""
    disc = track = None
    title, length = '', 0
    try:
        m = mutagen.File(path, easy=True)
        if m is not None:
            length = m.info.length or 0
            tags = m.tags or {}
            def first(k):
                v = tags.get(k)
                return str(v[0]) if v else ''
            dn = re.match(r'\d+', first('discnumber'))
            tn = re.match(r'\d+', first('tracknumber'))
            disc = int(dn.group()) if dn else None
            track = int(tn.group()) if tn else None
            title = first('title')
    except Exception:
        pass
    name = os.path.basename(path)
    if track is None:
        nums = re.findall(r'(?<![\d])(\d{1,3})(?![\d])', os.path.splitext(name)[0])
        if nums:
            track = int(nums[0])
            if len(nums[0]) == 3 and disc is None:
                disc, track = int(nums[0][0]), int(nums[0][1:])
    if disc is None:
        parent = os.path.basename(os.path.dirname(path))
        dm = DISC_DIR.match(parent)
        if dm:
            disc = int(re.search(r'\d+', parent).group())
    return {'disc': disc or 1, 'track': track or 0, 'title': title or clean_stem(name), 'length': length}


def stage(paths_by_disc, album_id):
    """Copy downloaded files into a staging folder we own, so tags and permissions can be written."""
    dest_root = os.path.join(STAGING, str(album_id))
    os.makedirs(dest_root, exist_ok=True)
    staged = []
    for src, subdir in paths_by_disc:
        d = os.path.join(dest_root, subdir) if subdir else dest_root
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, os.path.basename(src))
        shutil.copyfile(src, dst)
        staged.append((src, dst))
    return dest_root, staged


def remove_sources(staged):
    dirs = set()
    for src, _ in staged:
        try:
            os.remove(src)
            dirs.add(os.path.dirname(src))
        except OSError as e:
            warn(f'could not remove {src}: {e.strerror}')
    for d in sorted(dirs, key=len, reverse=True):
        while d.startswith(DOWNLOADS + '/') and d not in (DOWNLOADS, STAGING):
            try:
                left = [x for x in os.listdir(d) if not x.startswith('._') and x != '.DS_Store']
                if left:
                    break
                shutil.rmtree(d)
            except OSError:
                break
            d = os.path.dirname(d)


# ---------------------------------------------------------------- Lidarr import

def map_files(files, release_tracks, mi_items):
    """Map each audio file to a track of the chosen release. Returns {path: track}."""
    by_rec = defaultdict(list)
    for t in release_tracks:
        by_rec[t.get('foreignRecordingId')].append(t)
    mapping, used = {}, set()
    # 1. Lidarr's own guess, carried over by recording (works even when it guessed another release).
    for p in files:
        guess = (mi_items.get(p) or {}).get('tracks') or []
        if len(guess) != 1:
            continue
        for t in by_rec.get(guess[0].get('foreignRecordingId'), []):
            if t['id'] not in used:
                mapping[p] = t
                used.add(t['id'])
                break
    if len(mapping) != len(files):
        # 2. Disc/track order.
        info = {p: file_info(p) for p in files}
        ordered = sorted(files, key=lambda p: (info[p]['disc'], info[p]['track'], p))
        rt = sorted(release_tracks, key=lambda t: (t['mediumNumber'], t['absoluteTrackNumber']))
        mapping = dict(zip(ordered, rt))
    return mapping


def check_mapping(mapping):
    rows, ok = [], 0
    for p, t in mapping.items():
        fi = file_info(p)
        s = max(title_in(t['title'], fi['title']), title_in(t['title'], clean_stem(os.path.basename(p))))
        dd = abs(fi['length'] - t['duration'] / 1000) if fi['length'] and t.get('duration') else None
        good = s >= 0.6 or (dd is not None and dd <= 3)
        ok += good
        rows.append((good, os.path.basename(p), t['title'], s, dd))
    return ok / max(len(rows), 1), rows


def import_album(lid, artist, album, folder, exts):
    files = sorted(audio_files(folder, exts))
    if not files:
        raise RuntimeError(f'no audio files in {folder}')
    rel = choose_release(album, len(files))
    if not rel:
        counts = sorted({r['trackCount'] for r in album['releases']})
        raise RuntimeError(f'{len(files)} files, but no release of "{album["title"]}" has that many tracks (releases have {counts})')
    if release_tier(rel.get('format')) > 0:
        warn(f'no normal CD release with {len(files)} tracks; using {describe_release(rel)}')
    log(f'  release: {describe_release(rel)} ({", ".join(rel.get("country") or []) or "?"})')
    rtracks = lid.release_tracks(rel['id'])
    mi = lid.req('GET', 'manualimport', {'folder': folder, 'artistId': artist['id'],
                                         'filterExistingFiles': 'true', 'replaceExistingFiles': 'false'})
    items = {i['path']: i for i in mi or []}
    missing = [p for p in files if p not in items]
    if missing:
        raise RuntimeError(f'Lidarr did not see {len(missing)} file(s), e.g. {missing[0]}')
    mapping = map_files(files, rtracks, items)
    ratio, rows = check_mapping(mapping)
    if len(mapping) != len(files) or ratio < 0.9:
        for good, fname, ttitle, s, dd in rows:
            mark = green('ok') if good else red('??')
            log(f'    {mark} {fname}  ->  {ttitle}  ({s:.2f}{"" if dd is None else f", {dd:.0f}s off"})')
        raise RuntimeError('files do not line up with the release track list; import it by hand')
    for i in items.values():
        for r in i.get('rejections') or []:
            warn(f'Lidarr note on {os.path.basename(i["path"])}: {r.get("reason")}')

    lid.select_release(album, rel['id'])
    payload = [{'path': p, 'artistId': artist['id'], 'albumId': album['id'], 'albumReleaseId': rel['id'],
                'trackIds': [t['id']], 'quality': items[p]['quality'], 'indexerFlags': 0,
                'downloadId': '', 'additionalFile': False, 'replaceExistingFiles': False,
                'disableReleaseSwitching': True}
               for p, t in mapping.items()]
    lid.run_command({'name': 'ManualImport', 'files': payload, 'importMode': 'move',
                     'replaceExistingFiles': False})
    a = lid.album(album['id'])
    have = (a.get('statistics') or {}).get('trackFileCount', 0)
    if have < len(files):
        raise RuntimeError(f'Lidarr imported {have} of {len(files)} files')
    return a


# ---------------------------------------------------------------- MusicBrainz + Wikipedia

def mb(path):
    return ext_get(f'https://musicbrainz.org/ws/2/{path}{"&" if "?" in path else "?"}fmt=json', 'mb', gap=1.1)


def wiki_text(title):
    q = urllib.parse.urlencode({'action': 'parse', 'page': title, 'prop': 'wikitext', 'section': 0,
                                'redirects': 1, 'format': 'json', 'formatversion': 2})
    r = ext_get(f'https://en.wikipedia.org/w/api.php?{q}', 'wiki', gap=0.5)
    p = (r or {}).get('parse') or {}
    return p.get('title'), p.get('wikitext') or ''


def infobox_field(wt, field):
    m = re.search(r'\|\s*' + field + r'\s*=(.*?)(?=\n\s*\|\s*[\w ]+?\s*=|\n\s*\}\})', wt, re.S | re.I)
    return m.group(1).strip() if m else ''


MONTHS = {m: i for i, m in enumerate(['january', 'february', 'march', 'april', 'may', 'june', 'july',
                                       'august', 'september', 'october', 'november', 'december'], 1)}
MONTH_RE = '(' + '|'.join(MONTHS) + r'|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\.?'


def month_num(s):
    s = s.lower().rstrip('.')
    for name, n in MONTHS.items():
        if name.startswith(s):
            return n
    return None


def parse_wiki_date(val):
    """First release date in an infobox 'released' value -> 'YYYY-MM-DD' / 'YYYY-MM' / 'YYYY'."""
    v = re.sub(r'<!--.*?-->', '', val, flags=re.S)
    v = re.sub(r'<ref[^>]*/>', '', v)
    v = re.sub(r'<ref[^>]*>.*?</ref>', '', v, flags=re.S)
    v = re.sub(r'\[\[(?:[^|\]]*\|)?([^\]]*)\]\]', r'\1', v)
    full, partial = [], []
    for m in re.finditer(r'\{\{\s*(start date(?: and age)?|film date|release date(?: and age)?|dts|date)\s*\|([^{}]*)\}\}', v, re.I):
        params = [p.strip() for p in m.group(2).split('|') if '=' not in p]
        nums = []
        for p in params:
            if re.fullmatch(r'\d{1,4}', p):
                nums.append(int(p))
            else:
                break
        if len(nums) >= 3 and nums[0] > 1000:
            full.append((m.start(), f'{nums[0]:04d}-{nums[1]:02d}-{nums[2]:02d}'))
        elif len(nums) == 2 and nums[0] > 1000:
            partial.append((m.start(), f'{nums[0]:04d}-{nums[1]:02d}'))
        elif len(nums) == 1 and nums[0] > 1000:
            partial.append((m.start(), f'{nums[0]:04d}'))
        elif params:  # e.g. {{dts|November 18, 2002}}
            d = parse_wiki_date(params[0]) if not params[0].startswith('{{') else None
            if d:
                (full if len(d) == 10 else partial).append((m.start(), d))
    for m in re.finditer(MONTH_RE + r'\s+(\d{1,2}),?\s+(\d{4})', v, re.I):
        full.append((m.start(), f'{int(m.group(3)):04d}-{month_num(m.group(1)):02d}-{int(m.group(2)):02d}'))
    for m in re.finditer(r'(\d{1,2})\s+' + MONTH_RE + r',?\s+(\d{4})', v, re.I):
        full.append((m.start(), f'{int(m.group(3)):04d}-{month_num(m.group(2)):02d}-{int(m.group(1)):02d}'))
    for m in re.finditer(r'(\d{4})-(\d{2})-(\d{2})', v):
        full.append((m.start(), m.group(0)))
    if full:
        return min(full)[1]
    for m in re.finditer(MONTH_RE + r',?\s+(\d{4})', v, re.I):
        partial.append((m.start(), f'{int(m.group(2)):04d}-{month_num(m.group(1)):02d}'))
    for m in re.finditer(r'\b(1[89]\d\d|20\d\d)\b', v):
        partial.append((m.start(), m.group(1)))
    return min(partial)[1] if partial else None


def wikipedia_date(rg, album_title, artist_name):
    """Release date from the album's English Wikipedia infobox. Returns (date, page url)."""
    title = None
    for r in rg.get('relations') or []:
        url = (r.get('url') or {}).get('resource') or ''
        if r.get('type') == 'wikipedia' and 'en.wikipedia.org/wiki/' in url:
            title = urllib.parse.unquote(url.split('/wiki/', 1)[1])
    if not title:
        for r in rg.get('relations') or []:
            url = (r.get('url') or {}).get('resource') or ''
            if r.get('type') == 'wikidata':
                qid = url.rstrip('/').rsplit('/', 1)[-1]
                try:
                    d = ext_get(f'https://www.wikidata.org/wiki/Special:EntityData/{qid}.json', 'wikidata', gap=0.5)
                    title = (((d['entities'].get(qid) or {}).get('sitelinks') or {}).get('enwiki') or {}).get('title')
                except (HttpError, KeyError):
                    pass
    candidates = [title] if title else []
    if not title:
        q = urllib.parse.urlencode({'action': 'query', 'list': 'search', 'srlimit': 4, 'format': 'json',
                                    'srsearch': f'{album_title} {artist_name} album'})
        try:
            r = ext_get(f'https://en.wikipedia.org/w/api.php?{q}', 'wiki', gap=0.5)
            candidates = [x['title'] for x in r['query']['search']]
        except (HttpError, KeyError):
            candidates = []
    for cand in candidates:
        try:
            page, wt = wiki_text(cand)
        except HttpError:
            continue
        if not re.search(r'\{\{\s*infobox album', wt, re.I):
            continue
        if not title:  # found by search: make sure it is this artist's album
            box_artist = infobox_field(wt, 'artist')
            box_name = infobox_field(wt, 'name') or re.sub(r'\s*\(.*\)$', '', page or '')
            if norm(artist_name) not in norm(box_artist) or sim(box_name, album_title) < 0.85:
                continue
        d = parse_wiki_date(infobox_field(wt, 'released'))
        if d:
            return d, 'https://en.wikipedia.org/wiki/' + urllib.parse.quote((page or cand).replace(' ', '_'))
    return None, None


def choose_date(wiki, mb_date, rg_date):
    base = mb_date or rg_date
    if wiki and len(wiki) == 10:
        return wiki, 'Wikipedia'
    if wiki:
        if base and base.startswith(wiki):
            return base, 'MusicBrainz'
        return wiki, 'Wikipedia (partial)'
    return base, 'MusicBrainz'


# ---------------------------------------------------------------- cover art

def itunes_artist_albums(artist, country):
    """Every album iTunes lists for this artist. Text search misses some (e.g. Kill 'Em All)."""
    q = urllib.parse.urlencode({'term': artist, 'entity': 'musicArtist', 'limit': 10, 'country': country})
    try:
        res = ext_get(f'https://itunes.apple.com/search?{q}', 'itunes', gap=3) or {}
        ids = [r['artistId'] for r in res.get('results') or [] if norm(r.get('artistName')) == norm(artist)]
        if not ids:
            return []
        q = urllib.parse.urlencode({'id': ids[0], 'entity': 'album', 'limit': 200, 'country': country})
        res = ext_get(f'https://itunes.apple.com/lookup?{q}', 'itunes', gap=3) or {}
        return [r for r in res.get('results') or [] if r.get('wrapperType') == 'collection']
    except (HttpError, KeyError):
        return []


def itunes_cover(artist, album_title, track_count, year, country):
    def strip(name):
        name = re.sub(r'\s+-\s+(Single|EP)$', '', name)
        extra = re.findall(r'\s*[\(\[]([^\)\]]*)[\)\]]', name)
        return re.sub(r'\s*[\(\[][^\)\]]*[\)\]]', '', name).strip(), extra

    def score(r):
        if sim(r.get('artistName'), artist) < 0.85 and norm(artist) not in norm(r.get('artistName')):
            return 0.0
        name = r.get('collectionName') or ''
        base, extra = strip(name)
        s = max(sim(base, album_title), sim(name, album_title))
        if s < 0.8:
            return 0.0
        for e in extra:  # "(Deluxe Edition)" etc. that the Lidarr title doesn't have
            if norm(e) not in norm(album_title):
                s -= 0.05
        if re.search(r' - (Single|EP)$', name) and not re.search(r'\b(EP|Single)\b', album_title):
            s -= 0.15
        if track_count and r.get('trackCount'):
            s -= min(abs(r['trackCount'] - track_count), 5) * 0.02
        if year and r.get('releaseDate', '')[:4].isdigit():
            s -= min(abs(int(r['releaseDate'][:4]) - year), 5) * 0.01
        return s

    best, best_s = None, 0.0
    sources = [lambda: itunes_artist_albums(artist, country)]
    for term in (f'{artist} {album_title}', album_title):
        q = urllib.parse.urlencode({'term': term, 'entity': 'album', 'limit': 50, 'country': country})
        sources.append(lambda q=q: (ext_get(f'https://itunes.apple.com/search?{q}', 'itunes', gap=3) or {}).get('results') or [])
    for source in sources:
        try:
            results = source()
        except HttpError:
            continue
        for r in results:
            s = score(r)
            if s > best_s:
                best, best_s = r, s
        if best and best_s >= 0.85:
            break
    if not best or best_s < 0.7:
        return None, None
    # Same source as the iTunes artwork finder. Apple serves the largest size it has (usually
    # 3000x3000 or more) when asked for 9999x9999; the old 100000x100000 trick is refused now.
    urls = [re.sub(r'/\d+x\d+bb\.(jpg|png)$', f'/{s}bb.jpg', best['artworkUrl100']) for s in ('9999x9999', '3000x3000')]
    return urls, best.get('collectionName')


def save_cover(data, path):
    img = Image.open(io.BytesIO(data))
    if img.format == 'JPEG':
        with open(path, 'wb') as f:
            f.write(data)
    else:
        img.convert('RGB').save(path, 'JPEG', quality=95)
    return Image.open(path).size


def ensure_cover(album_dir, artist, album, rel, country, force):
    path = os.path.join(album_dir, 'cover.jpg')
    if os.path.exists(path) and not force:
        return path, 'already there'
    year = int(album['releaseDate'][:4]) if album.get('releaseDate') else None
    urls, name = itunes_cover(artist, album['title'], rel.get('trackCount'), year, country)
    for url in urls or []:
        try:
            w, h = save_cover(ext_get(url, 'mzstatic', raw=True, tries=2), path)
            return path, f'iTunes "{name}" {w}x{h}'
        except (HttpError, OSError) as e:
            warn(f'iTunes art download failed: {e}')
    for u in (f'https://coverartarchive.org/release/{rel["foreignReleaseId"]}/front',
              f'https://coverartarchive.org/release-group/{album["foreignAlbumId"]}/front'):
        try:
            w, h = save_cover(ext_get(u, 'caa', raw=True), path)
            return path, yellow(f'Cover Art Archive {w}x{h} (no iTunes match)')
        except (HttpError, OSError):
            continue
    return None, red('none found')


# ---------------------------------------------------------------- Picard-style tags

PICARD_ROLES = {'arranger': 'arranger', 'instrument arranger': 'arranger', 'vocal arranger': 'arranger',
                'orchestrator': 'arranger', 'audio': 'engineer', 'engineer': 'engineer',
                'recording': 'engineer', 'sound': 'engineer', 'mastering': 'engineer',
                'balance': 'engineer', 'live sound': 'engineer', 'mix': 'mixer', 'producer': 'producer',
                'remixer': 'remixer', 'conductor': 'conductor', 'composer': 'composer',
                'lyricist': 'lyricist', 'librettist': 'lyricist', 'writer': 'writer', 'mix-DJ': 'djmixer'}


def add_rels(rels, out):
    for r in rels or []:
        if r.get('target-type') != 'artist' or not r.get('artist'):
            continue
        name = r.get('target-credit') or r['artist']['name']
        typ = r.get('type')
        attrs = [a for a in r.get('attributes') or [] if a not in ('additional', 'guest', 'solo', 'task')]
        if typ == 'instrument':
            out['performer'].append(f'{name} ({", ".join(attrs) or "instruments"})')
        elif typ == 'vocal':
            out['performer'].append(f'{name} ({", ".join(attrs) or "vocals"})')
        elif typ == 'performer':
            out['performer'].append(f'{name} ({", ".join(attrs)})' if attrs else name)
        elif typ in PICARD_ROLES:
            out[PICARD_ROLES[typ]].append(name)


def credit(ac):
    phrase = ''.join(a.get('name', '') + (a.get('joinphrase') or '') for a in ac or [])
    sort = ''.join((a.get('artist') or {}).get('sort-name', a.get('name', '')) + (a.get('joinphrase') or '') for a in ac or [])
    return phrase, sort, [a.get('name', '') for a in ac or []], [(a.get('artist') or {}).get('id', '') for a in ac or []]


def uniq(xs):
    seen, out = set(), []
    for x in xs:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def title_case_genre(name):
    return ' '.join('-'.join(p[:1].upper() + p[1:] for p in w.split('-')) for w in name.split(' '))


def build_album_tags(release, rg, date):
    phrase, sort, names, ids = credit(release.get('artist-credit'))
    rgp = release.get('release-group') or {}
    types = [t.lower() for t in ([rgp.get('primary-type')] + (rgp.get('secondary-types') or [])) if t]
    labels = release.get('label-info') or []
    tr = release.get('text-representation') or {}
    orig = rg.get('first-release-date') or rgp.get('first-release-date') or ''
    counts = Counter()
    for o in (release, rg):
        for g in o.get('genres') or []:
            counts[g['name']] += g.get('count') or 1
    t = {
        'album': release.get('title'),
        'albumartist': phrase, 'albumartistsort': sort, 'musicbrainz_albumartistid': ids,
        'musicbrainz_albumid': release.get('id'), 'musicbrainz_releasegroupid': rgp.get('id') or rg.get('id'),
        'releasetype': types, 'musicbrainz_albumtype': types,
        'releasestatus': (release.get('status') or '').lower(),
        'musicbrainz_albumstatus': (release.get('status') or '').lower(),
        'releasecountry': release.get('country'), 'barcode': release.get('barcode'),
        'asin': release.get('asin'),
        'label': uniq((l.get('label') or {}).get('name') for l in labels),
        'catalognumber': uniq(l.get('catalog-number') for l in labels),
        'script': tr.get('script'), 'language': tr.get('language'),
        'date': date, 'originaldate': orig, 'originalyear': orig[:4],
        'totaldiscs': str(len(release.get('media') or [])), 'disctotal': str(len(release.get('media') or [])),
        'genre': [title_case_genre(n) for n, _ in counts.most_common(7)],
    }
    return t


def build_track_tags(release, medium, track):
    rec = track.get('recording') or {}
    ac = track.get('artist-credit') or rec.get('artist-credit') or release.get('artist-credit')
    phrase, sort, names, ids = credit(ac)
    roles = defaultdict(list)
    add_rels(release.get('relations'), roles)
    add_rels(rec.get('relations'), roles)
    for r in rec.get('relations') or []:
        if r.get('target-type') == 'work' and r.get('work'):
            add_rels(r['work'].get('relations'), roles)
    t = {
        'title': track.get('title') or rec.get('title'),
        'artist': phrase, 'artists': names, 'artistsort': sort, 'musicbrainz_artistid': ids,
        'musicbrainz_trackid': rec.get('id'), 'musicbrainz_releasetrackid': track.get('id'),
        'isrc': rec.get('isrcs') or [],
        'tracknumber': str(track.get('position')),
        'totaltracks': str(medium.get('track-count') or len(medium.get('tracks') or [])),
        'tracktotal': str(medium.get('track-count') or len(medium.get('tracks') or [])),
        'discnumber': str(medium.get('position')),
        'media': medium.get('format'), 'discsubtitle': medium.get('title'),
    }
    for k, v in roles.items():
        t[k] = uniq(v)
    return t


def write_tags(path, tags, art):
    f = FLAC(path)
    keep = {k: v for k, v in (f.tags or {}).items() if KEEP_TAGS.match(k)} if f.tags else {}
    if f.tags is None:
        f.add_tags()
    f.tags.clear()
    for k, v in keep.items():
        f.tags[k] = v
    for k, v in tags.items():
        vals = [str(x) for x in (v if isinstance(v, list) else [v]) if x not in (None, '')]
        if vals:
            f.tags[k] = vals
    if art:
        f.clear_pictures()
        f.add_picture(art)
    f.save()


def embedded_art(cover_path, size=500):
    if not cover_path:
        return None
    img = Image.open(cover_path).convert('RGB')
    img.thumbnail((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=90)
    pic = Picture()
    pic.type, pic.mime, pic.desc = 3, 'image/jpeg', ''
    pic.width, pic.height, pic.depth = img.width, img.height, 24
    pic.data = buf.getvalue()
    return pic


# ---------------------------------------------------------------- lyrics (LRCLIB, like LRCGET)

def same_title(a, b):
    """Whole-title match, ignoring extras like "(Remastered 2011)" or " - Live"."""
    def base(t):
        t = re.sub(r'\s*[\(\[][^\)\]]*[\)\]]', '', t or '')
        return re.sub(r'\s+-\s+.*$', '', t)
    return sim(base(a), base(b)) >= 0.85 or sim(a, b) >= 0.85


def fetch_lyrics(artist, title, album, seconds):
    """LRCLIB lyrics, synced if any copy has them.

    First the exact lookup lrcget uses (artist, title, album, length). If that is missing,
    or has only plain lyrics, also search by artist + title: LRCLIB often holds several
    copies of a song and another one may be synced."""
    q = urllib.parse.urlencode({'artist_name': artist, 'track_name': title, 'album_name': album,
                                'duration': round(seconds)})
    exact = None
    try:
        exact = ext_get(f'https://lrclib.net/api/get?{q}', 'lrclib', gap=0.2)
    except HttpError as e:
        if e.code != 404:
            raise
    if exact and (exact.get('syncedLyrics') or exact.get('instrumental')):
        return exact
    q = urllib.parse.urlencode({'artist_name': artist, 'track_name': title})
    try:
        res = ext_get(f'https://lrclib.net/api/search?{q}', 'lrclib', gap=0.2) or []
    except (HttpError, urllib.error.URLError, TimeoutError, ConnectionError, ValueError):
        if exact:
            return exact  # keep the plain copy rather than fail
        raise
    res = [r for r in res if r.get('duration') and abs(r['duration'] - seconds) <= 2
           and same_title(title, r.get('trackName'))
           and (norm(artist) in norm(r.get('artistName')) or sim(artist, r.get('artistName')) >= 0.8)]
    res.sort(key=lambda r: (not r.get('syncedLyrics'), not r.get('plainLyrics'), abs(r['duration'] - seconds)))
    if res and res[0].get('syncedLyrics'):
        return res[0]
    return exact or (res[0] if res else None)


def write_lyrics(path, artist, title, album, seconds):
    base = os.path.splitext(path)[0]
    if os.path.exists(base + '.lrc'):
        return 'kept'
    upgrade = os.path.exists(base + '.txt')  # plain lyrics already there: only look for synced ones
    try:
        r = fetch_lyrics(artist, title, album, seconds)
    except (HttpError, urllib.error.URLError, TimeoutError, ConnectionError, ValueError):
        return 'kept' if upgrade else 'error'
    if upgrade:
        if not r or not r.get('syncedLyrics') or r.get('instrumental'):
            return 'kept'
        with open(base + '.lrc', 'w', encoding='utf-8') as f:
            f.write(r['syncedLyrics'])
        os.remove(base + '.txt')
        return 'synced (upgraded)'
    if not r:
        return 'none'
    if r.get('instrumental'):
        text, ext, kind = '[au: instrumental]', '.lrc', 'instrumental'
    elif r.get('syncedLyrics'):
        text, ext, kind = r['syncedLyrics'], '.lrc', 'synced'
    elif r.get('plainLyrics'):
        text, ext, kind = r['plainLyrics'], '.txt', 'plain'
    else:
        return 'none'
    with open(base + ext, 'w', encoding='utf-8') as f:
        f.write(text)
    return kind


def lyrics_with_retries(jobs, pause=5):
    """LRCLIB lookups often fail at random, so keep re-running the ones that failed
    until a whole pass recovers none of them. jobs: list of (path, artist, title, album, seconds)."""
    results = {j[0]: write_lyrics(*j) for j in jobs}
    passes = 1
    while True:
        failed = [j for j in jobs if results[j[0]] in ('error', 'none')]
        if not failed:
            break
        time.sleep(pause)
        passes += 1
        recovered = 0
        for j in failed:
            results[j[0]] = write_lyrics(*j)
            recovered += results[j[0]] not in ('error', 'none')
        if not recovered:
            break
    counts = Counter(results.values())
    if counts.get('none') or counts.get('error'):
        counts['missing'] = counts.pop('none', 0) + counts.pop('error', 0)
    return counts, passes


# ---------------------------------------------------------------- permissions + Jellyfin

def fix_perms(album_dir):
    gid, problems = os.getgid(), 0
    targets = [os.path.dirname(album_dir)]
    for root, dirs, files in os.walk(album_dir):
        targets.append(root)
        targets += [os.path.join(root, f) for f in files]
    for p in targets:
        try:
            st = os.stat(p)
            if st.st_uid == os.getuid() and st.st_gid != gid:
                os.chown(p, -1, gid)
            add = 0o070 if os.path.isdir(p) else 0o060
            if st.st_mode & add != add:
                os.chmod(p, st.st_mode | add)
        except PermissionError:
            problems += 1
    return problems


def jellyfin_refresh(cfg):
    if not cfg['jellyfin_key']:
        warn('Jellyfin scan skipped: put JELLYFIN_API_KEY in .env')
        return
    h = {'Authorization': f'MediaBrowser Token="{cfg["jellyfin_key"]}"'}
    base = cfg['jellyfin_url']
    try:
        folders = http('GET', f'{base}/Library/VirtualFolders', h) or []
        music = [f for f in folders if any(l.rstrip('/') == MUSIC for l in f.get('Locations') or [])]
        if music:
            q = urllib.parse.urlencode({'Recursive': 'true', 'ImageRefreshMode': 'Default',
                                        'MetadataRefreshMode': 'Default', 'ReplaceAllImages': 'false',
                                        'ReplaceAllMetadata': 'false'})
            for f in music:
                http('POST', f'{base}/Items/{f["ItemId"]}/Refresh?{q}', h)
            log(green(f'Jellyfin: scanning {", ".join(f["Name"] for f in music)}'))
        else:
            http('POST', f'{base}/Library/Refresh', h)
            log(green('Jellyfin: scanning all libraries'))
    except (HttpError, urllib.error.URLError) as e:
        warn(f'Jellyfin scan failed: {e}')


# ---------------------------------------------------------------- finishing an imported album

def finish_album(cfg, lid, artist, album_id, force_cover=False, exts=LOSSLESS):
    album = lid.album(album_id)
    rel = next((r for r in album['releases'] if r['monitored']), None)
    if not rel:
        raise RuntimeError('album has no selected release in Lidarr')
    tracks = lid.album_tracks(album_id)
    tfs = {tf['id']: tf for tf in lid.trackfiles(album_id)}
    pairs = [(t, tfs[t['trackFileId']]['path']) for t in tracks if t.get('trackFileId') in tfs]
    if not pairs:
        raise RuntimeError('no files imported for this album')
    dirs = {os.path.dirname(p) for _, p in pairs}
    album_dir = os.path.commonpath(list(dirs)) if len(dirs) > 1 else dirs.pop()
    rep = {'album': album['title'], 'dir': album_dir, 'host_dir': host_path(album_dir)}

    release = mb(f'release/{rel["foreignReleaseId"]}?inc=artists+artist-credits+labels+recordings+release-groups'
                 f'+media+isrcs+genres+artist-rels+work-rels+recording-level-rels+work-level-rels')
    rg = mb(f'release-group/{album["foreignAlbumId"]}?inc=genres+url-rels')

    override = (ALBUM_NOTES.get(album_id) or {}).get('override')
    if override:
        date, src, wiki_url = override, 'set by you', None
    else:
        wiki, wiki_url = wikipedia_date(rg, album['title'], artist['artistName'])
        date, src = choose_date(wiki, release.get('date'), rg.get('first-release-date'))
    ALBUM_NOTES.update(album_id, date=date, date_src=src)
    rep.update(date=date, date_src=src, wiki_url=wiki_url, mb_date=release.get('date'))
    folder_year = re.search(r'\((\d{4})\)\s*$', album_dir)
    if date and folder_year and folder_year.group(1) != date[:4]:
        rep['year_note'] = (f'folder says {folder_year.group(1)} but {src} says {date[:4]}; fix the date on '
                            f'MusicBrainz and refresh the artist in Lidarr to rename the folder')

    cover, cover_src = ensure_cover(album_dir, artist['artistName'], album, rel, cfg['itunes_country'], force_cover)
    rep['cover'] = cover_src
    art = embedded_art(cover)

    by_id, by_pos = {}, {}
    for medium in release.get('media') or []:
        for t in medium.get('tracks') or []:
            by_id[t['id']] = (medium, t)
            by_pos[(medium.get('position'), t.get('position'))] = (medium, t)
    album_tags = build_album_tags(release, rg, date)
    lyric_jobs = []
    tagged = 0
    for t, path in sorted(pairs, key=lambda x: (x[0]['mediumNumber'], x[0]['absoluteTrackNumber'])):
        mt = by_id.get(t.get('foreignTrackId')) or by_pos.get((t['mediumNumber'], t['absoluteTrackNumber']))
        if path.lower().endswith('.flac') and mt:
            tags = dict(album_tags)
            tags.update(build_track_tags(release, *mt))
            write_tags(path, tags, art)
            tagged += 1
        elif not mt:
            warn(f'no MusicBrainz track for {os.path.basename(path)}; tags left as they were')
        title = mt[1]['title'] if mt else t['title']
        artist_name = credit((mt[1].get('artist-credit') if mt else None) or release.get('artist-credit'))[0]
        seconds = (mutagen.File(path).info.length if os.path.exists(path) else t['duration'] / 1000)
        lyric_jobs.append((path, artist_name or artist['artistName'], title, release.get('title'), seconds))
    rep['tagged'] = f'{tagged}/{len(pairs)}'
    lyr, passes = lyrics_with_retries(lyric_jobs)
    rep['lyrics'] = ', '.join(f'{v} {k}' for k, v in lyr.most_common()) + (f' ({passes} passes)' if passes > 1 else '')
    bad = fix_perms(album_dir)
    if bad:
        rep['perms'] = f'{bad} item(s) not owned by you; run: sudo chmod -R g+rw "{host_path(album_dir)}"'
    return rep


def host_path(p):
    for h, c in HOST_PATHS.items():
        if p == c or p.startswith(c + '/'):
            return h + p[len(c):]
    return p


def container_path(p):
    for h, c in HOST_PATHS.items():
        if p == h or p.startswith(h + '/'):
            return c + p[len(h):]
    return p


def print_report(rep):
    log(f'  date    {rep.get("date")}  {dim("(" + rep.get("date_src", "") + ")")}'
        + (f'  {dim(rep["wiki_url"])}' if rep.get('wiki_url') else ''))
    if rep.get('year_note'):
        warn(rep['year_note'])
    log(f'  cover   {rep.get("cover")}')
    log(f'  tags    {rep.get("tagged")} files')
    log(f'  lyrics  {rep.get("lyrics")}')
    if rep.get('perms'):
        warn(rep['perms'])


# ---------------------------------------------------------------- jobs (shared by the CLI and the web app)

SEARCH_WORKERS = 4  # slskd searches running side by side
ACTIVE = ('picked', 'queued', 'downloaded', 'pending', 'processing')


class JobStore:
    """jobs.json, guarded by a file lock so the CLI, the web app and its worker can share it."""

    @contextmanager
    def locked(self):
        os.makedirs(STATE, exist_ok=True)
        with open(JOBS_FILE + '.lock', 'a') as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)

    def _load(self):
        try:
            with open(JOBS_FILE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def _save(self, jobs):
        tmp = JOBS_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(jobs, f, indent=1)
        os.replace(tmp, JOBS_FILE)

    def all(self):
        with self.locked():
            return self._load()

    def get(self, jid):
        return next((j for j in self.all() if j['id'] == jid), None)

    def add(self, *new):
        with self.locked():
            jobs = self._load()
            jobs.extend(new)
            self._save(jobs)

    def update(self, jid, **fields):
        with self.locked():
            jobs = self._load()
            for j in jobs:
                if j['id'] == jid:
                    j.update(fields)
                    self._save(jobs)
                    return j

    def append_log(self, jid, lines):
        with self.locked():
            jobs = self._load()
            for j in jobs:
                if j['id'] == jid:
                    j['log'] = (j.get('log') or []) + list(lines)
                    j['log'] = j['log'][-400:]
                    self._save(jobs)
                    return

    def remove(self, jid):
        with self.locked():
            self._save([j for j in self._load() if j['id'] != jid])

    def remove_finished(self):
        with self.locked():
            self._save([j for j in self._load() if j['status'] in ACTIVE])


JOBS = JobStore()

DATE_RE = re.compile(r'^\d{4}(-\d{2}(-\d{2})?)?$')


class JsonDict:
    """A small JSON file of {album id: {...}} in state/, safe to share between processes."""
    def __init__(self, name):
        self.path = os.path.join(STATE, name)

    @contextmanager
    def locked(self):
        os.makedirs(STATE, exist_ok=True)
        with open(self.path + '.lock', 'a') as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)

    def _load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def all(self):
        with self.locked():
            return self._load()

    def get(self, album_id):
        return self.all().get(str(album_id))

    def _save(self, data):
        tmp = self.path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.path)

    def update(self, album_id, **fields):
        with self.locked():
            data = self._load()
            n = data.setdefault(str(album_id), {})
            n.update(fields)
            for k in [k for k, v in n.items() if v is None]:
                del n[k]
            self._save(data)

    def remove(self, album_id, only_if=None):
        """Drop an album's entry (only if its fields match only_if, when given)."""
        with self.locked():
            data = self._load()
            cur = data.get(str(album_id))
            if cur is None or (only_if and any(cur.get(k) != v for k, v in only_if.items())):
                return False
            del data[str(album_id)]
            self._save(data)
            return True


ALBUM_NOTES = JsonDict('albums.json')   # date last written, and any date set by hand
SEARCHES = JsonDict('searches.json')    # the slskd search behind each album's results
SEARCH_MAX_AGE = 3 * 86400              # unused searches are cleared after this
SETTINGS = JsonDict('settings.json')    # app settings, e.g. {"format": {"value": "cd"}}


def wanted_format():
    v = (SETTINGS.get('format') or {}).get('value')
    return v if v in FORMATS else 'cd'


def set_wanted_format(v):
    if v not in FORMATS:
        raise ValueError(f'format must be one of {", ".join(FORMATS)}')
    SETTINGS.update('format', value=v)


def remember_search(album_id, sid, text):
    """Keep an album's slskd search; deletes the one it replaces."""
    old = SEARCHES.get(album_id)
    SEARCHES.update(album_id, sid=sid, text=text, at=time.time())
    return old['sid'] if old and old.get('sid') != sid else None


def forget_search(slsk, album_id, sid=None):
    """Delete an album's slskd search (once it is in the library, or replaced)."""
    cur = SEARCHES.get(album_id) or {}
    sid = sid or cur.get('sid')
    if slsk and sid:
        slsk.delete_search(sid)
    if sid and cur.get('sid') == sid:
        SEARCHES.remove(album_id, {'sid': sid})


def set_date_override(album_id, date):
    """date 'YYYY[-MM[-DD]]' to pin it, or None to go back to the Wikipedia/MusicBrainz date."""
    if date is not None and not DATE_RE.match(date):
        raise ValueError('date must look like 1988-08-25 (or 1988-08, or 1988)')
    ALBUM_NOTES.update(album_id, override=date)


def new_job(kind, **fields):
    j = {'id': str(uuid.uuid4()), 'kind': kind, 'status': 'picked' if kind == 'download' else 'pending',
         'created': time.time(), 'log': []}
    j.update(fields)
    return j


def download_job(artist, album, cand, allow_lossy=False, search_id=None):
    return new_job('download', artist_id=artist['id'], artist=artist['artistName'],
                   album_id=album['id'], album=album['title'], username=cand['username'], dir=cand['dir'],
                   files=[{'filename': f['filename'], 'size': f['size']} for f in cand['files']],
                   allow_lossy=bool(allow_lossy), search_id=search_id)


def acquire_runner_lock():
    """Only one process may run downloads. Returns an open lock file, or None if another has it."""
    os.makedirs(STATE, exist_ok=True)
    fd = open(os.path.join(STATE, 'runner.lock'), 'a')
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        fd.close()
        return None


class Capture:
    """Collect log() output of the current thread (for a job's log in the web app)."""
    def __enter__(self):
        self.lines = []
        _tls.sink = self.lines
        return self

    def __exit__(self, *exc):
        _tls.sink = None


class Runner:
    def __init__(self, cfg, stall_minutes=30):
        self.cfg = cfg
        self.lid = Lidarr(cfg['lidarr_url'], cfg['lidarr_key'])
        self.slsk = Slskd(cfg['slskd_url'], cfg['slskd_key']) if cfg.get('slskd_key') else None
        self.stall = stall_minutes * 60
        self.finished = 0
        for j in JOBS.all():  # a previous run was killed mid-way
            if j['status'] == 'processing':
                JOBS.update(j['id'], status='failed', error='interrupted (musicflow was restarted); retry it')

    def event(self, j, msg, color=None):
        log((color or (lambda s: s))(f'{msg}  {j.get("album") or ""}'))
        JOBS.append_log(j['id'], [f'{time.strftime("%H:%M:%S")} {msg}'])

    def clear_old_searches(self):
        if time.time() - getattr(self, '_cleared_at', 0) < 3600:
            return
        self._cleared_at = time.time()
        active = {j.get('album_id') for j in JOBS.all() if j['status'] in ACTIVE}
        for aid, s in SEARCHES.all().items():
            if int(aid) not in active and time.time() - (s.get('at') or 0) > SEARCH_MAX_AGE:
                forget_search(self.slsk, aid, s.get('sid'))

    def step(self):
        """One pass: start downloads, check them, then process at most one ready job.
        Returns the number of jobs still active."""
        self.clear_old_searches()
        jobs = JOBS.all()
        self._start(jobs)
        self._poll(jobs)
        jobs = JOBS.all()
        ready = [j for j in jobs if (j['kind'] == 'download' and j['status'] == 'downloaded')
                 or (j['kind'] != 'download' and j['status'] == 'pending')]
        if ready:
            self._process(ready[0])
        active = [j for j in JOBS.all() if j['status'] in ACTIVE]
        if not active and self.finished:
            jellyfin_refresh(self.cfg)
            self.finished = 0
        return len(active)

    def _dest_dirs(self, j):
        return {os.path.dirname(local_path(f['filename'])) for f in j.get('files') or []}

    def _start(self, jobs):
        if not self.slsk:
            return
        busy = set()
        for j in jobs:
            if j['kind'] == 'download' and j['status'] == 'queued':
                busy |= self._dest_dirs(j)
        for j in jobs:
            if j['kind'] != 'download' or j['status'] != 'picked':
                continue
            if self._dest_dirs(j) & busy:
                continue  # same download folder as a running job; wait for it
            try:
                self.slsk.enqueue(j['username'], j['files'])
                now = time.time()
                JOBS.update(j['id'], status='queued', queued_at=now, last_progress=now, done_bytes=0, pct=0)
                busy |= self._dest_dirs(j)
                self.event(j, f'queued from {j["username"]}', dim)
            except (HttpError, urllib.error.URLError) as e:
                JOBS.update(j['id'], status='failed', error=f'could not queue download: {e}')
                self.event(j, f'failed to queue: {e}', red)

    def _poll(self, jobs):
        if not self.slsk:
            return
        per_user = {}
        for j in jobs:
            if j['kind'] != 'download' or j['status'] != 'queued':
                continue
            u = j['username']
            if u not in per_user:
                try:
                    per_user[u] = self.slsk.transfers(u)
                except (HttpError, urllib.error.URLError) as e:
                    warn(f'slskd: {e}')
                    per_user[u] = None
            tr = per_user[u]
            if tr is None:
                continue
            states = [tr.get(f['filename']) for f in j['files']]
            done = sum((s or {}).get('bytesTransferred') or 0 for s in states)
            total = sum(f['size'] for f in j['files']) or 1
            fields = {'pct': round(100 * done / total, 1), 'done_bytes': done,
                      'speed': sum((s or {}).get('averageSpeed') or 0 for s in states
                                   if s and s.get('state') == 'InProgress'),
                      'state': Counter((s or {}).get('state', 'Requested') for s in states).most_common(1)[0][0]}
            if done > (j.get('done_bytes') or 0):
                fields['last_progress'] = time.time()
            failed = [s for s in states if s and s['state'].startswith('Completed') and 'Succeeded' not in s['state']]
            if all(s and s['state'] == 'Completed, Succeeded' for s in states):
                fields.update(status='downloaded', pct=100)
                self.event(j, 'downloaded', green)
                self.slsk.clear(u, states)
            elif failed:
                fields.update(status='failed', error=f'{len(failed)} file(s) {failed[0]["state"].split(", ")[-1].lower()} on {u}\'s side; pick another source')
            elif time.time() - fields.get('last_progress', j.get('last_progress') or time.time()) > self.stall:
                fields.update(status='failed', error=f'no progress for {self.stall // 60} min; pick another source')
            if fields.get('status') == 'failed':
                self.slsk.clear(u, states)
                self.event(j, f'failed: {fields["error"]}', red)
            JOBS.update(j['id'], **fields)

    def _process(self, j):
        JOBS.update(j['id'], status='processing', started_at=time.time())
        log()
        log(bold(f'{j.get("artist")} - {j.get("album") or "(several albums)"}'))
        with Capture() as cap:
            try:
                if j['kind'] == 'download':
                    reports = [process_download(self.cfg, self.lid, j)]
                elif j['kind'] == 'import':
                    reports = [process_import(self.cfg, self.lid, j)]
                else:
                    artist = self.lid.req('GET', f'artist/{j["artist_id"]}')
                    reports = []
                    for aid in j['album_ids']:
                        a = self.lid.album(aid)
                        log(bold(f'{a["title"]}'))
                        try:
                            rep = finish_album(self.cfg, self.lid, artist, aid, j.get('force_cover'))
                            print_report(rep)
                            reports.append(rep)
                        except Exception as e:
                            log(red(f'  failed: {e}'))
                            reports.append({'album': a['title'], 'error': str(e)})
                if j['kind'] != 'finish':
                    print_report(reports[0])
                status, err = 'done', None
                if any(r.get('error') for r in reports):
                    status, err = 'failed', '; '.join(f'{r["album"]}: {r["error"]}' for r in reports if r.get('error'))
                self.finished += 1
            except Exception as e:  # keep going with the other jobs
                log(red(f'  failed: {e}'))
                reports, status, err = [], 'failed', str(e)
        JOBS.update(j['id'], status=status, error=err, reports=reports, finished_at=time.time())
        if status == 'done' and j['kind'] in ('download', 'import') and j.get('album_id'):
            # The album is in; its search is no longer needed. A failed job keeps it so you
            # can pick another source without searching again.
            forget_search(self.slsk, j['album_id'], j.get('search_id'))
            forget_search(self.slsk, j['album_id'])
        JOBS.append_log(j['id'], cap.lines)


def process_download(cfg, lid, j):
    exts = LOSSLESS | (LOSSY if j.get('allow_lossy') else set())
    srcs = []
    for f in j['files']:
        p = find_downloaded(f['filename'], f['size'])
        if not p:
            raise RuntimeError(f'downloaded file not found: {host_path(local_path(f["filename"]))}')
        parts = f['filename'].replace('/', '\\').split('\\')
        parent = parts[-2] if len(parts) > 1 else ''
        srcs.append((p, parent if DISC_DIR.match(parent) else ''))
    staged_dir, staged = stage(srcs, j['album_id'])
    return import_and_finish(cfg, lid, j['artist_id'], j['album_id'], staged_dir, staged, exts)


def process_import(cfg, lid, j):
    exts = LOSSLESS | (LOSSY if j.get('allow_lossy') else set())
    folder = j['folder']
    files = sorted(audio_files(folder, exts))
    if not files:
        raise RuntimeError(f'no audio files in {host_path(folder)}')
    srcs = []
    for p in files:
        rel = os.path.relpath(os.path.dirname(p), folder)
        srcs.append((p, '' if rel == '.' else rel))
    staged_dir, staged = stage(srcs, j['album_id'])
    return import_and_finish(cfg, lid, j['artist_id'], j['album_id'], staged_dir, staged, exts, j.get('force_cover'))


def import_and_finish(cfg, lid, artist_id, album_id, staged_dir, staged, exts, force_cover=False):
    artist = lid.req('GET', f'artist/{artist_id}')
    album = lid.album(album_id)
    try:
        import_album(lid, artist, album, staged_dir, exts)
    except Exception:
        shutil.rmtree(staged_dir, ignore_errors=True)  # the downloaded originals are untouched
        raise
    remove_sources(staged)
    shutil.rmtree(staged_dir, ignore_errors=True)
    log(green('  imported'))
    return finish_album(cfg, lid, artist, album_id, force_cover, exts)


def guess_album(lid, artist, folder, title=None):
    albums = lid.albums(artist['id'])
    if title:
        return max(albums, key=lambda a: sim(a['title'], title))
    mi = lid.req('GET', 'manualimport', {'folder': folder, 'artistId': artist['id'],
                                         'filterExistingFiles': 'true', 'replaceExistingFiles': 'false'})
    votes = Counter((i.get('album') or {}).get('id') for i in mi or [] if i.get('album'))
    if votes:
        best = votes.most_common(1)[0][0]
        for a in albums:
            if a['id'] == best:
                return a
    return max(albums, key=lambda a: sim(a['title'], os.path.basename(folder.rstrip('/'))))


def missing_albums(lid, artist, title=None):
    """(missing, partial) monitored, released albums without an active job."""
    albums = [a for a in lid.albums(artist['id']) if a['monitored']]
    if title:
        albums = [a for a in albums if sim(a['title'], title) > 0.8 or norm(title) in norm(a['title'])]
    today = time.strftime('%Y-%m-%d')
    active = {j.get('album_id') for j in JOBS.all() if j['status'] in ACTIVE}
    missing, partial = [], []
    for a in sorted(albums, key=lambda a: a.get('releaseDate') or ''):
        st = a.get('statistics') or {}
        if (a.get('releaseDate') or '9999')[:10] > today or a['id'] in active:
            continue
        if st.get('trackFileCount', 0) == 0:
            missing.append(a)
        elif st.get('trackFileCount', 0) < st.get('trackCount', 0):
            partial.append(a)
    return missing, partial


def rank(responses, lid, album, release, exts, wanted=None):
    tracks = lid.release_tracks(release['id'])
    alt = {r['trackCount'] for r in album['releases']}
    cands = [score_candidate(c, tracks, alt, album['title']) for c in build_candidates(responses, exts)]
    return apply_format(cands, wanted or wanted_format())


def search_album(slsk, lid, artist_name, album, exts, text=None, release=None, wanted=None):
    """Search slskd for one album. Returns (search text, ranked candidates, target release, search id).
    The search is left in slskd; delete it once the album is in (or no longer wanted)."""
    release = release or choose_release(album)
    if not release:
        raise RuntimeError('album has no releases in Lidarr')
    default = search_text(artist_name, album['title'])
    text = text or default
    sid, responses = slsk.search(text)
    cands = rank(responses, lid, album, release, exts, wanted)
    if not cands and text == default:
        short = search_text('', re.sub(r'\s*[\(\[].*?[\)\]]', '', album['title']))
        if len(short.split()) >= 2:
            slsk.delete_search(sid)
            return search_album(slsk, lid, artist_name, album, exts, short, release, wanted)
    return text, cands, release, sid


# ---------------------------------------------------------------- commands

def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        return 'q'


def pick_interactive(artist, albums, slsk, lid, exts, top, wanted=None):
    """Search every album (a few at a time, ahead of you) and let you confirm each pick.
    Returns [(album, candidate, search id)]. Searches of albums you skip are deleted."""
    picks, sids = [], {}

    def search(a, text=None):
        text, cands, rel, sid = search_album(slsk, lid, artist['artistName'], a, exts, text, wanted=wanted)
        old = sids.get(a['id'])
        sids[a['id']] = sid
        slsk.delete_search(old)
        return text, cands

    try:
        with ThreadPoolExecutor(max_workers=SEARCH_WORKERS) as pool:
            futures = [pool.submit(search, a) for a in albums]
            for i, (a, fut) in enumerate(zip(albums, futures), 1):
                rel = choose_release(a)
                log()
                log(bold(f'[{i}/{len(albums)}] {artist["artistName"]} - {a["title"]} ({(a.get("releaseDate") or "????")[:4]})')
                    + (dim(f'   want: {describe_release(rel)}') if rel else ''))
                try:
                    text, cands = fut.result()
                except Exception as e:
                    warn(f'search failed: {e}')
                    text, cands = search_text(artist['artistName'], a['title']), []
                shown = top
                while True:
                    if not cands:
                        log(yellow(f'  no {"/".join(sorted(exts))} results for "{text}"'))
                    for j, c in enumerate(cands[:shown], 1):
                        log(describe_candidate(j, c))
                    hint = 'Enter=1, number=pick, ' if cands else ''
                    choice = ask(dim(f'  {hint}s=skip, m=more, /words=search again, q=stop picking > '))
                    if choice == '' and cands:
                        picks.append((a, cands[0], sids.get(a['id'])))
                        break
                    if choice.isdigit() and 1 <= int(choice) <= min(shown, len(cands)):
                        picks.append((a, cands[int(choice) - 1], sids.get(a['id'])))
                        break
                    if choice == 's':
                        break
                    if choice == 'm':
                        shown += top
                        continue
                    if choice.startswith('/') and len(choice) > 1:
                        log(dim(f'  searching "{choice[1:]}"...'))
                        try:
                            text, cands = search(a, choice[1:])
                        except Exception as e:
                            warn(f'search failed: {e}')
                        shown = top
                        continue
                    if choice == 'q':
                        for f in futures:
                            f.cancel()
                        return picks
        return picks
    finally:
        keep = {sid for _, _, sid in picks}
        for sid in sids.values():
            if sid not in keep:
                slsk.delete_search(sid)


def run_until_idle(cfg, args, ids=None):
    lock = acquire_runner_lock()
    if not lock:
        log(green('The musicflow web app is running and will download these; follow them in its Activity tab.'))
        return
    runner = Runner(cfg, args.stall_minutes)
    start = time.time()
    last = 0
    while True:
        active = runner.step()
        if not active:
            break
        if time.time() - last > 60:
            dl = [j for j in JOBS.all() if j['status'] == 'queued']
            if dl:
                log(dim('  downloading: ' + ' | '.join(f'{j["album"][:28]} {j.get("pct", 0):.0f}%' for j in dl)))
            last = time.time()
        time.sleep(10)
    done = [j for j in JOBS.all() if (j.get('finished_at') or 0) >= start and (ids is None or j['id'] in ids)]
    if done:
        log()
        log(bold('Summary'))
        for j in done:
            if j['status'] == 'done':
                for r in j.get('reports') or []:
                    log(f'  {green("OK")}      {r["album"]}: date {r.get("date")} ({r.get("date_src")}), '
                        f'cover {r.get("cover")}, lyrics {r.get("lyrics")}')
                    if r.get('year_note'):
                        log(f'          {yellow(r["year_note"])}')
            else:
                log(f'  {red("FAILED")}  {j.get("album")}: {j.get("error")}')


def cmd_get(cfg, args):
    lid = Lidarr(cfg['lidarr_url'], cfg['lidarr_key'])
    if not cfg['slskd_key']:
        sys.exit('No slskd API key found: set SLSKD_API_KEY or SLSKD_CONFIG in .env.')
    slsk = Slskd(cfg['slskd_url'], cfg['slskd_key'])
    exts = LOSSLESS | (LOSSY if args.allow_lossy else set())
    artist = lid.find_artist(args.artist)
    wanted = args.format or wanted_format()
    todo, partial = missing_albums(lid, artist, args.album)
    log(bold(f'{artist["artistName"]}: {len(todo)} missing album(s)') + dim(f'   format: {FORMATS[wanted]}'))
    for a in partial:
        warn(f'{a["title"]} is partly downloaded; skipped (finish it by hand or delete its files first)')
    if not todo:
        return
    picks = pick_interactive(artist, todo, slsk, lid, exts, args.top, wanted)
    if not picks:
        log('Nothing picked.')
        return
    if args.dry_run:
        for _, _, sid in picks:
            slsk.delete_search(sid)
        log(bold(f'\nDry run: {len(picks)} pick(s), nothing downloaded.'))
        return
    jobs = []
    for a, c, sid in picks:
        jobs.append(download_job(artist, a, c, args.allow_lossy, sid))
        slsk.delete_search(remember_search(a['id'], sid, None))
    JOBS.add(*jobs)
    run_until_idle(cfg, args, {j['id'] for j in jobs})


def cmd_import(cfg, args):
    lid = Lidarr(cfg['lidarr_url'], cfg['lidarr_key'])
    artist = lid.find_artist(args.artist)
    folder = container_path(args.folder)
    if not os.path.isdir(folder):
        sys.exit(f'Not a folder: {args.folder}. Use a path under {host_path(DOWNLOADS)}.')
    album = guess_album(lid, artist, folder, args.album)
    log(bold(f'{artist["artistName"]} - {album["title"]}') + dim(f'   from {host_path(folder)}'))
    if not args.yes and ask(dim('  import this as that album? [Y/n] ')).lower().startswith('n'):
        return
    j = new_job('import', artist_id=artist['id'], artist=artist['artistName'], album_id=album['id'],
                album=album['title'], folder=folder, allow_lossy=bool(args.allow_lossy),
                force_cover=bool(args.force_cover))
    rep = process_import(cfg, lid, j)
    print_report(rep)
    if not args.no_jellyfin:
        jellyfin_refresh(cfg)


def cmd_finish(cfg, args):
    lid = Lidarr(cfg['lidarr_url'], cfg['lidarr_key'])
    artist = lid.find_artist(args.artist)
    albums = [a for a in lid.albums(artist['id']) if (a.get('statistics') or {}).get('trackFileCount')]
    if args.album:
        albums = [a for a in albums if sim(a['title'], args.album) > 0.8 or norm(args.album) in norm(a['title'])]
    if not albums:
        sys.exit('No imported albums matched.')
    if args.date:
        if len(albums) != 1:
            sys.exit('--date needs --album to match exactly one album; matched: ' + ', '.join(a['title'] for a in albums))
        set_date_override(albums[0]['id'], None if args.date.lower() == 'wikipedia' else args.date)
    for a in albums:
        log(bold(f'{artist["artistName"]} - {a["title"]}'))
        try:
            print_report(finish_album(cfg, lid, artist, a['id'], args.force_cover))
        except Exception as e:
            log(red(f'  failed: {e}'))
    if not args.no_jellyfin:
        jellyfin_refresh(cfg)


def main():
    p = argparse.ArgumentParser(prog='musicflow', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    g = sub.add_parser('get', help='download and finish all missing albums for an artist')
    g.add_argument('artist')
    g.add_argument('--album', help='only albums matching this title')
    g.add_argument('--dry-run', action='store_true', help='search and pick, but download nothing')
    g.add_argument('--allow-lossy', action='store_true', help='also offer MP3/AAC/OGG results')
    g.add_argument('--top', type=int, default=5, help='results shown per album (default 5)')
    g.add_argument('--format', choices=list(FORMATS),
                   help='wanted format: cd (16/44.1), hires (24-bit) or any; default is the saved setting')
    g.add_argument('--stall-minutes', type=int, default=30, help='give up on a source after this long without progress')
    r = sub.add_parser('resume', help='continue downloads from an earlier get')
    r.add_argument('--stall-minutes', type=int, default=30)
    i = sub.add_parser('import', help='import a folder you downloaded yourself, then finish it')
    i.add_argument('artist')
    i.add_argument('folder', help='a folder in your slskd downloads, e.g. "/srv/slskd/downloads/Artist - Album"')
    i.add_argument('--album', help='Lidarr album title, if the guess is wrong')
    i.add_argument('--yes', '-y', action='store_true', help="don't ask before importing")
    i.add_argument('--allow-lossy', action='store_true')
    i.add_argument('--force-cover', action='store_true')
    i.add_argument('--no-jellyfin', action='store_true')
    f = sub.add_parser('finish', help='redo cover, tags, lyrics and permissions on imported albums')
    f.add_argument('artist')
    f.add_argument('--album')
    f.add_argument('--force-cover', action='store_true', help='replace an existing cover.jpg')
    f.add_argument('--date', help='pin the date tag, e.g. 1988-08-25 (needs --album); "wikipedia" undoes it')
    f.add_argument('--no-jellyfin', action='store_true')
    s = sub.add_parser('format', help='show or set the wanted format (shared with the web app)')
    s.add_argument('value', nargs='?', choices=list(FORMATS))
    args = p.parse_args()
    os.umask(0o002)
    cfg = load_config()
    try:
        if args.cmd == 'get':
            cmd_get(cfg, args)
        elif args.cmd == 'resume':
            run_until_idle(cfg, args)
        elif args.cmd == 'import':
            cmd_import(cfg, args)
        elif args.cmd == 'finish':
            cmd_finish(cfg, args)
        elif args.cmd == 'format':
            if args.value:
                set_wanted_format(args.value)
            log(f'wanted format: {FORMATS[wanted_format()]}')
    except KeyboardInterrupt:
        log(yellow('\nStopped. Downloads already queued keep going in slskd; run `musicflow resume` '
                   '(or open the web app) to finish them.'))
        sys.exit(130)


if __name__ == '__main__':
    main()

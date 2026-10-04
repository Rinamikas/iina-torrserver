#!/usr/bin/env python3
"""Optional persistent file cache for IINA TorrServer Companion. Python 3.9+."""
import argparse
import contextlib
import fcntl
import hmac
import json
import logging
import math
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, ProxyHandler, HTTPRedirectHandler, build_opener

VIDEO = {'mkv', 'mp4', 'avi', 'mov', 'm4v', 'ts', 'm2ts', 'mts', 'mpg',
         'mpeg', 'webm', 'wmv', 'flv', 'ogv', 'vob'}
HASH = re.compile(r'^[0-9a-f]{40}$')
CHUNK = 128 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def natural_key(value):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r'(\d+)', value)]


def byte_range(header, size):
    if not header:
        return 0, size - 1
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
    if not match or not any(match.groups()) or size <= 0:
        raise ValueError('Invalid range')
    first, last = match.groups()
    if not first:
        count = int(last)
        if count <= 0:
            raise ValueError('Invalid suffix')
        return max(0, size - count), size - 1
    first = int(first)
    last = min(int(last), size - 1) if last else size - 1
    if first >= size or last < first:
        raise ValueError('Unsatisfiable range')
    return first, last


def safe_name(value):
    value = ''.join('_' if c in '/\\' else c for c in value
                    if ord(c) >= 32 and ord(c) != 127).strip()
    return '' if value in ('.', '..') else value


class Companion:
    def __init__(self, config, clock=time.time):
        self.config = config
        self.clock = clock
        for name in ('download_enabled', 'cleanup_watched', 'cleanup_expired'):
            value = config.get(name, False)
            if type(value) is not bool:
                raise ValueError(name + ' must be a JSON boolean')
            setattr(self, name, value)
        self.origin = config['torrserver'].rstrip('/')
        parsed = urlsplit(self.origin)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.path:
            raise ValueError('torrserver must be an HTTP(S) origin')
        if (not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', config.get('token', ''))
                or config['token'].startswith('REPLACE_')):
            raise ValueError('Set a random token of at least 32 characters')
        self.root = Path(config['cache_dir']).expanduser().absolute()
        if self.root.is_symlink():
            raise ValueError('Cache directory must not be a symlink')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        marker = self.root / '.iina-torrserver-cache'
        if not marker.exists():
            if any(self.root.iterdir()):
                raise ValueError('Refusing to adopt a nonempty unmarked cache directory')
            marker.write_text('IINA TorrServer Companion\n')
        if marker.is_symlink() or marker.read_text() != 'IINA TorrServer Companion\n':
            raise ValueError('Invalid cache marker')
        self.root = self.root.resolve()
        self.lock = threading.RLock()
        self.metadata_lock = threading.Lock()
        self.state_path = self.root / 'state.json'
        if self.state_path.is_symlink():
            raise ValueError('Symlink cache state')
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        if not isinstance(self.state, dict) or any(not HASH.fullmatch(h) for h in self.state):
            raise ValueError('Invalid cache state; refusing to clean anything')
        for torrent in self.state.values():
            if (not isinstance(torrent, dict) or type(torrent.get('last_use')) not in (int, float)
                    or not math.isfinite(torrent['last_use']) or not isinstance(torrent.get('files', []), list)
                    or not isinstance(torrent.get('progress', {}), dict)):
                raise ValueError('Invalid torrent cache state')
            ids = set()
            for file in torrent.get('files', []):
                if (not isinstance(file, dict) or type(file.get('id')) is not int or file['id'] < 1
                        or file['id'] in ids or type(file.get('length')) is not int or file['length'] < 0
                        or not isinstance(file.get('path'), str) or not isinstance(file.get('dav'), str)
                        or not file['dav'].startswith('/dav/')):
                    raise ValueError('Invalid file cache state')
                ids.add(file['id'])
        self.sessions = {}
        self.readers = {}
        self.downloading = None
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.started = self.clock()
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.max_bytes = int(config.get('max_cache_bytes', 64 * 1024**3))
        self.reserve = int(config.get('min_free_bytes', 10 * 1024**3))
        self.ttl = float(config.get('retention_seconds', 7 * 86400))
        self.lease = float(config.get('lease_seconds', 120))
        if self.max_bytes <= 0 or self.reserve < 0 or self.ttl <= 0 or self.lease <= 0:
            raise ValueError('Invalid cache limits')

    def persist(self):
        temporary = self.state_path.with_suffix('.tmp')
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(self.state, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(self.state_path))

    def path(self, torrent_hash, index, create=False):
        if not HASH.fullmatch(torrent_hash) or not isinstance(index, int) or index < 1:
            raise ValueError('Invalid file identity')
        parent = self.root / torrent_hash
        if parent.is_symlink():
            raise ValueError('Symlink cache directory')
        if create:
            parent.mkdir(mode=0o700, exist_ok=True)
        target = parent / (str(index) + '.cache')
        if target.is_symlink():
            raise ValueError('Symlink cache file')
        return target

    def available(self, torrent_hash, file):
        target = self.path(torrent_hash, file['id'])
        size = target.stat().st_size if target.exists() else 0
        if size > file['length']:
            raise ValueError('Cache exceeds the declared file size')
        return size

    def api(self, path, payload):
        request = Request(self.origin + path, json.dumps(payload).encode(),
                          {'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=10) as response:
            body = response.read(2 * 1024**2)
            return json.loads(body) if body else None

    def catalog(self, torrent_hash):
        if not HASH.fullmatch(torrent_hash):
            raise ValueError('Invalid torrent hash')
        with self.metadata_lock:
            with self.lock:
                known = self.state.get(torrent_hash)
                if known and known.get('files'):
                    return known
            # Read metadata only. Neither this endpoint nor WebDAV marks a file viewed.
            with self.opener.open(self.origin + '/stream?link=' + torrent_hash + '&stat',
                                  timeout=15) as response:
                detail = json.loads(response.read(2 * 1024**2))
            if detail.get('hash') != torrent_hash:
                raise ValueError('Mismatched torrent metadata')
            entries = detail.get('file_stats')
            if not isinstance(entries, list) or not entries:
                raise ValueError('Torrent has no file metadata')
            files, ids = [], set()
            for entry in entries:
                index, name, length = entry.get('id'), entry.get('path'), entry.get('length')
                if (type(index) is not int or index < 1 or index in ids
                        or type(length) is not int or length < 0 or not isinstance(name, str)
                        or not name or any(p in ('', '.', '..') for p in name.split('/'))
                        or '\\' in name or any(ord(c) < 32 for c in name)):
                    raise ValueError('Unsafe torrent file metadata')
                ids.add(index)
                files.append({'id': index, 'path': name, 'length': length,
                              'position': 0, 'duration': 0, 'watched': False})
            # WebDAV paths are based on category/title. Refuse ambiguous torrent titles.
            category = safe_name(detail.get('category', '')) or 'other'
            title = safe_name(detail.get('title', '')) or torrent_hash
            torrents = self.api('/torrents', {'action': 'list'})
            matches = [t for t in torrents if (safe_name(t.get('category', '')) or 'other') == category
                       and (safe_name(t.get('title', '')) or t['hash']) == title]
            if len(matches) != 1 or matches[0]['hash'] != torrent_hash:
                raise ValueError('Ambiguous WebDAV torrent title')
            # File.Path includes the torrent root; DisplayPath omits it for multi-file torrents.
            for file in files:
                relative = file['path'].split('/', 1)[-1]
                file['dav'] = '/dav/' + '/'.join(quote(p, safe='') for p in
                                               [category, title] + relative.split('/'))
            with self.lock:
                known = self.state.setdefault(torrent_hash, {'last_use': self.clock(), 'expired': False})
                known['files'] = sorted(files, key=lambda f: natural_key(f['path']))
                self.persist()
                return known

    def event(self, payload):
        torrent_hash, index = payload.get('hash', ''), payload.get('index')
        client, event = payload.get('client'), payload.get('event')
        if (not isinstance(torrent_hash, str) or not HASH.fullmatch(torrent_hash)
                or type(index) is not int or index < 1
                or not isinstance(client, str) or not 1 <= len(client) <= 128
                or event not in ('start', 'heartbeat', 'end')):
            raise ValueError('Invalid playback event')
        position, duration = payload.get('position', 0), payload.get('duration', 0)
        if (type(position) not in (int, float) or type(duration) not in (int, float)
                or not math.isfinite(position) or not math.isfinite(duration)
                or position < 0 or duration < 0 or position > duration + 1 and duration > 0):
            raise ValueError('Invalid playback position')
        with self.lock:
            now = self.clock()
            torrent = self.state.setdefault(torrent_hash, {'last_use': now, 'expired': False, 'files': []})
            torrent['last_use'] = now
            torrent['expired'] = False
            torrent['current'] = index
            if event == 'end':
                self.sessions.pop(client, None)
            else:
                self.sessions[client] = (torrent_hash, index, now)
            # Keep progress even when metadata retrieval is still pending.
            progress = torrent.setdefault('progress', {})
            previous = progress.get(str(index), {})
            if position > 0 or duration > 0:
                progress[str(index)] = {'position': position, 'duration': duration,
                    'pending': payload.get('sync') is True and position > 0,
                    'watched': previous.get('watched', False) or duration > 0 and position / duration >= .95}
            self.persist()
        self.wake.set()

    def sync_progress(self):
        with self.lock:
            pending = [(h, index, dict(p)) for h, t in self.state.items()
                       for index, p in t.get('progress', {}).items() if p.get('pending')]
        for torrent_hash, index, progress in pending:
            self.api('/viewed', {'action': 'set', 'hash': torrent_hash,
                               'file_index': int(index), 'timecode': progress['position']})
            with self.lock:
                current = self.state[torrent_hash]['progress'][index]
                if current == progress:
                    current['pending'] = False
                    self.persist()

    def watched(self, torrent, file):
        progress = torrent.get('progress', {})
        if progress.get(str(file['id']), {}).get('watched', False):
            return True
        if file['path'].rsplit('.', 1)[-1].lower() in VIDEO:
            return False
        owners = [video for video in torrent.get('files', [])
                  if video['path'].rsplit('.', 1)[-1].lower() in VIDEO
                  and file['path'].startswith(video['path'].rsplit('.', 1)[0] + '.')]
        return bool(owners) and all(progress.get(str(video['id']), {}).get('watched', False) for video in owners)

    def active(self, torrent_hash):
        now = self.clock()
        return (self.readers.get(torrent_hash, 0) > 0 or any(
            h == torrent_hash and now - stamp < self.lease for h, _, stamp in self.sessions.values()))

    def usage(self):
        return sum(p.stat().st_size for parent in self.root.iterdir()
                   if parent.is_dir() and not parent.is_symlink() and HASH.fullmatch(parent.name)
                   for p in parent.iterdir() if p.is_file() and not p.is_symlink())

    def cleanup(self):
        if not (self.cleanup_watched or self.cleanup_expired):
            return []
        with self.lock:
            # After a restart, give existing players time to renew their leases.
            if self.clock() - self.started < self.lease:
                return []
            removed = []
            changed = False
            for torrent_hash, torrent in self.state.items():
                if self.active(torrent_hash):
                    continue
                expired = self.cleanup_expired and self.clock() - torrent['last_use'] >= self.ttl
                for file in torrent.get('files', []):
                    if self.downloading == (torrent_hash, file['id']):
                        continue
                    if expired or (self.cleanup_watched and self.watched(torrent, file)):
                        target = self.path(torrent_hash, file['id'])
                        if target.exists():
                            target.unlink()
                            removed.append((torrent_hash, file['id']))
                if expired:
                    changed = changed or not torrent.get('expired')
                    torrent['expired'] = True
            if removed or changed:
                self.persist()
            if removed:
                logging.info('Cleaned %s cached files', len(removed))
            return removed

    def choose_download(self):
        if not self.download_enabled:
            return
        with self.lock:
            for torrent_hash, torrent in sorted(self.state.items(), key=lambda item: -item[1]['last_use']):
                if self.cleanup_expired and (torrent.get('expired')
                        or self.clock() - torrent['last_use'] >= self.ttl):
                    continue
                files = torrent.get('files', [])
                # Download the playing file first, then remaining unwatched files.
                files = sorted(files, key=lambda f: f['id'] != torrent.get('current'))
                for file in files:
                    if not self.watched(torrent, file) and self.available(torrent_hash, file) < file['length']:
                        return torrent_hash, file

    def open_range(self, url, start, end):
        response = self.opener.open(Request(self.origin + url,
            headers={'Range': 'bytes=%d-%d' % (start, end), 'Accept-Encoding': 'identity'}), timeout=15)
        expected = 'bytes %d-%d/' % (start, end)
        if response.status != 206 or not response.headers.get('Content-Range', '').startswith(expected):
            response.close()
            raise ValueError('Upstream did not honor the requested byte range')
        return response

    def download_one(self, torrent_hash, file):
        if not self.download_enabled:
            return
        with self.lock:
            self.downloading = (torrent_hash, file['id'])
        try:
            position = self.available(torrent_hash, file)
            while position < file['length'] and not self.stopping.is_set():
                self.cleanup()
                with self.lock:
                    torrent = self.state[torrent_hash]
                    if (not self.download_enabled or self.watched(torrent, file)
                            or (self.cleanup_expired and (torrent.get('expired')
                                or self.clock() - torrent['last_use'] >= self.ttl))):
                        break
                    count = min(4 * 1024**2, file['length'] - position)
                    if self.usage() + count > self.max_bytes or shutil.disk_usage(self.root).free - count < self.reserve:
                        logging.info('Background download paused: cache or free-space limit')
                        break
                # Bounded requests keep cancellation, disk limits and retries predictable.
                with self.open_range(file['dav'], position, position + count - 1) as response:
                    data = response.read(count)
                    if len(data) != count:
                        raise IOError('Truncated upstream response')
                with self.lock:
                    if self.watched(self.state[torrent_hash], file) or self.stopping.is_set():
                        break
                    target = self.path(torrent_hash, file['id'], create=True)
                    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, 'ab') as output:
                        output.write(data)
                        output.flush()
                        os.fsync(output.fileno())
                    position += len(data)
        finally:
            with self.lock:
                self.downloading = None

    def run(self):
        while not self.stopping.is_set():
            try:
                with self.lock:
                    pending = [h for h, t in self.state.items() if not t.get('files') and not t.get('expired')]
                for torrent_hash in pending:
                    try:
                        self.catalog(torrent_hash)
                    except Exception as error:
                        logging.warning('Metadata retry: %s', type(error).__name__)
                try:
                    self.sync_progress()
                except Exception as error:
                    logging.warning('Progress sync retry: %s', type(error).__name__)
                self.cleanup()
                task = self.choose_download()
                if task:
                    self.download_one(*task)
                    # Continue with the next file, but back off quota/stall failures.
                    if self.available(*task) == task[1]['length']:
                        continue
            except Exception as error:
                logging.warning('Background retry: %s', type(error).__name__)
            self.wake.wait(10)
            self.wake.clear()

    def status(self):
        with self.lock:
            torrents = []
            for torrent_hash, torrent in self.state.items():
                files = [{'id': f['id'], 'path': f['path'], 'length': f['length'],
                          'cached': self.available(torrent_hash, f), 'watched': self.watched(torrent, f)}
                         for f in torrent.get('files', [])]
                torrents.append({'hash': torrent_hash, 'active': self.active(torrent_hash),
                                 'expired': torrent.get('expired', False), 'files': files,
                                 'pending_progress': sum(bool(p.get('pending')) for p in torrent.get('progress', {}).values())})
            return {'cache_bytes': self.usage(), 'max_cache_bytes': self.max_bytes,
                    'features': {name: getattr(self, name) for name in
                                 ('download_enabled', 'cleanup_watched', 'cleanup_expired')},
                    'retention_seconds': self.ttl, 'downloading': self.downloading, 'torrents': torrents}


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    @property
    def companion(self):
        return self.server.companion

    def log_message(self, *args):
        pass  # Never log playback URLs or credentials.

    def authorized(self):
        return hmac.compare_digest(self.headers.get('Authorization', '').encode(),
                                   ('Bearer ' + self.companion.config['token']).encode())

    def reply(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        if self.command != 'HEAD':
            self.wfile.write(body)

    def do_POST(self):
        if not self.authorized() or self.headers.get('Origin'):
            return self.reply(403, {'error': 'Forbidden'})
        if self.path != '/event':
            return self.reply(404, {'error': 'Unknown endpoint'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4096:
                raise ValueError('Invalid event size')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError('Invalid event')
            self.companion.event(payload)
            self.reply(200, {'ok': True})
        except (ValueError, TypeError, OverflowError):
            self.reply(400, {'error': 'Invalid playback event'})

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.authorized():
            return self.reply(403, {'error': 'Forbidden'})
        try:
            if self.path == '/status':
                return self.reply(200, self.companion.status())
            match = re.fullmatch(r'/catalog/([0-9a-f]{40})', self.path)
            if match:
                catalog = self.companion.catalog(match[1])
                return self.reply(200, {'hash': match[1], 'file_stats': catalog['files']})
            match = re.fullmatch(r'/media/([0-9a-f]{40})/(\d+)/[^/?]+', self.path)
            if not match:
                return self.reply(404, {'error': 'Unknown endpoint'})
            self.media(match[1], int(match[2]))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            logging.warning('Request failed: %s', type(error).__name__)
            if not getattr(self, 'media_headers_sent', False):
                self.reply(502, {'error': 'TorrServer/cache unavailable'})
            self.close_connection = True

    def media(self, torrent_hash, index):
        companion = self.companion
        torrent = companion.catalog(torrent_hash)
        file = next((f for f in torrent['files'] if f['id'] == index), None)
        if file is None:
            return self.reply(404, {'error': 'Unknown file'})
        size = file['length']
        try:
            start, end = byte_range(self.headers.get('Range'), size)
        except ValueError:
            self.send_response(416)
            self.send_header('Content-Range', 'bytes */%d' % size)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        with companion.lock:
            torrent['last_use'] = companion.clock()
            torrent['expired'] = False
            companion.readers[torrent_hash] = companion.readers.get(torrent_hash, 0) + 1
        try:
            with contextlib.ExitStack() as resources:
                position = start
                available = companion.available(torrent_hash, file)
                cached = None
                if start < available:
                    fd = os.open(str(companion.path(torrent_hash, index)), os.O_RDONLY | os.O_NOFOLLOW)
                    cached = resources.enter_context(os.fdopen(fd, 'rb'))
                    cached.seek(start)
                # Open upstream before sending headers, so IINA can fall back on a clean error.
                remote = None
                remote_start = max(start, available) if cached else start
                if self.command != 'HEAD' and remote_start <= end:
                    remote = resources.enter_context(companion.open_range(
                        '/stream?link=%s&index=%d&play' % (torrent_hash, index), remote_start, end))
                self.send_response(206 if self.headers.get('Range') else 200)
                self.send_header('Content-Type', mimetypes.guess_type(file['path'])[0] or 'application/octet-stream')
                self.send_header('Content-Length', str(max(0, end - start + 1)))
                self.send_header('Accept-Ranges', 'bytes')
                if self.headers.get('Range'):
                    self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.media_headers_sent = True
                self.close_connection = True
                if self.command == 'HEAD':
                    return
                while position <= end:
                    count = min(CHUNK, end - position + 1)
                    if cached and position < available:
                        data = cached.read(min(count, available - position))
                    else:
                        data = remote.read(count)
                    if not data:
                        raise IOError('Truncated playback response')
                    self.wfile.write(data)
                    position += len(data)
        finally:
            with companion.lock:
                companion.readers[torrent_hash] -= 1
            companion.wake.set()


def make_server(companion, bind='127.0.0.1', port=8092):
    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    server.companion = companion
    return server


def service_command_matches(command, target, config_path):
    suffix = ' %s --config %s' % (target, config_path)
    if not command.endswith(suffix):
        return False
    interpreter = Path(command[:-len(suffix)])
    return interpreter.is_absolute() and bool(re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', interpreter.name, re.I))


def install(args):
    if sys.platform != 'darwin':
        raise SystemExit('Automatic startup installation is supported on macOS only')
    os.umask(0o077)
    home = Path.home()
    root = home / 'Library/Application Support/iina-torrserver'
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / 'companion.json'
    if not config_path.exists():
        config = {'torrserver': args.server, 'bind': args.bind, 'port': args.port,
                  'download_enabled': False, 'cleanup_watched': False, 'cleanup_expired': False,
                  'token': secrets.token_urlsafe(32),
                  'cache_dir': str(home / 'Library/Caches/iina-torrserver'),
                  'max_cache_bytes': 64 * 1024**3, 'min_free_bytes': 10 * 1024**3,
                  'retention_seconds': 604800, 'lease_seconds': 120}
        config_path.write_text(json.dumps(config, indent=2) + '\n')
        config_path.chmod(0o600)
    config = json.loads(config_path.read_text())
    checked = Companion(config)  # Validate before changing startup.
    target = root / 'companion.py'
    lock_path = checked.root / '.service-lock'
    if lock_path.exists() and lock_path.read_text().strip().isdigit():
        pid = int(lock_path.read_text())
        process = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True)
        if process.returncode == 0 and service_command_matches(process.stdout.strip(), target, config_path):
            os.kill(pid, signal.SIGTERM)
            probe = os.open(str(lock_path), os.O_RDWR | os.O_NOFOLLOW)
            for _ in range(50):
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(probe, fcntl.LOCK_UN)
                    break
                except BlockingIOError:
                    pass
                time.sleep(.1)
            else:
                os.close(probe)
                raise RuntimeError('Existing companion did not stop; no forced termination')
            os.close(probe)
    if Path(__file__).resolve() != target.resolve():
        shutil.copyfile(__file__, target)
    # User cron works on a headless Mini without a GUI session or administrator access.
    # The file lock makes reboot/minute invocations harmless while a service is alive.
    cron_result = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
    cron = cron_result.stdout if cron_result.returncode == 0 else ''
    if cron_result.returncode and 'no crontab' not in cron_result.stderr:
        raise RuntimeError('Could not inspect crontab')
    backup = root / 'crontab.before-companion'
    if not backup.exists():
        backup.write_text(cron)
    command = ' '.join(shlex.quote(v) for v in [sys.executable, str(target), '--config', str(config_path)])
    command += ' >> ' + shlex.quote(str(root / 'companion.log')) + ' 2>&1 # iina-torrserver-companion'
    lines = [l for l in cron.splitlines() if not l.endswith('# iina-torrserver-companion')]
    subprocess.run(['crontab', '-'], input='\n'.join(lines + ['@reboot ' + command, '* * * * * ' + command]) + '\n', text=True, check=True)
    with (root / 'companion.log').open('ab') as log:
        subprocess.Popen([sys.executable, str(target), '--config', str(config_path)],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    host = config.get('bind', '127.0.0.1')
    if host == '0.0.0.0':
        host = '127.0.0.1'
    status_url = 'http://%s:%d/status' % (host, config.get('port', 8092))
    opener = build_opener(ProxyHandler({}), NoRedirect())
    for _ in range(30):
        try:
            request = Request(status_url, headers={'Authorization': 'Bearer ' + config['token']})
            with opener.open(request, timeout=.5) as response:
                status = json.load(response)
                assert status['max_cache_bytes'] == config['max_cache_bytes']
            break
        except (OSError, HTTPError):
            time.sleep(.1)
    else:
        raise RuntimeError('Companion startup did not pass the status check; inspect companion.log')
    print('Companion installed. Private configuration: ' + str(config_path))
    print('Copy its token into the IINA companion_token option; do not publish the token.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config')
    parser.add_argument('--install', action='store_true')
    parser.add_argument('--server', default='http://127.0.0.1:8090')
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8092)
    args = parser.parse_args()
    if args.install:
        return install(args)
    if not args.config:
        parser.error('--config is required unless --install is used')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    os.umask(0o077)
    config = json.loads(Path(args.config).expanduser().read_text())
    companion = Companion(config)
    lock_fd = os.open(str(companion.root / '.service-lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        return
    os.ftruncate(lock_fd, 0)
    os.write(lock_fd, str(os.getpid()).encode())
    server = make_server(companion, config.get('bind', '127.0.0.1'), int(config.get('port', 8092)))
    worker = threading.Thread(target=companion.run, daemon=True)
    worker.start()

    def stop(*_):
        companion.stopping.set()
        companion.wake.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    logging.info('IINA TorrServer cache companion started')
    try:
        server.serve_forever()
    finally:
        stop()
        worker.join(timeout=20)
        with companion.lock:
            companion.persist()
        server.server_close()
        os.close(lock_fd)


if __name__ == '__main__':
    main()

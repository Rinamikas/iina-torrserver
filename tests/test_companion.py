"""Deterministic cache/HTTP tests; no real downloads or production viewed records."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener

spec = importlib.util.spec_from_file_location('companion', Path(__file__).parents[1] / 'companion.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
HASH = 'a' * 40
TOKEN = 'test-only-not-a-secret-' * 3
DATA = {1: bytes(range(256)) * 1200, 2: b'episode two\n' * 30000, 3: b'subtitles'}
opener = build_opener(ProxyHandler({}))


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path == '/viewed':
            self.server.viewed[payload['file_index']] = payload['timecode']
            return self.send_json({})
        self.send_json([{'hash': HASH, 'title': 'Season / Unicode', 'category': 'tv'}])

    def send_json(self, value):
        data = json.dumps(value).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.endswith('&stat'):
            return self.send_json({'hash': HASH, 'title': 'Season / Unicode', 'category': 'tv',
                'file_stats': [{'id': 1, 'path': 'Season/S01E1.mkv', 'length': len(DATA[1])},
                               {'id': 2, 'path': 'Season/S01E2.mkv', 'length': len(DATA[2])},
                               {'id': 3, 'path': 'Season/S01E2.eng.srt', 'length': len(DATA[3])}]})
        index = 1
        if '/dav/' in self.path:
            index = 3 if self.path.endswith('srt') else 2 if 'E2' in self.path else 1
            self.server.dav_requests += 1
        else:
            self.server.play_requests += 1
            index = 2 if 'index=2' in self.path else 1
        data = DATA[index]
        first, last = module.byte_range(self.headers.get('Range'), len(data))
        content = data[first:last + 1]
        self.send_response(200 if self.server.ignore_range else 206)
        self.send_header('Content-Range', 'bytes %d-%d/%d' % (first, last, len(data)))
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        if self.server.truncate:
            content = content[:len(content) // 2]
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        cls.upstream.daemon_threads = True
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown()
        cls.upstream.server_close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = [1000.]
        self.config = {'torrserver': 'http://127.0.0.1:%d' % self.upstream.server_port,
                       'download_enabled': True, 'cleanup_watched': True, 'cleanup_expired': True,
                       'cache_dir': self.temp.name + '/cache', 'token': TOKEN,
                       'min_free_bytes': 0, 'max_cache_bytes': 10000000,
                       'retention_seconds': 604800, 'lease_seconds': 120}
        self.cache = module.Companion(self.config, clock=lambda: self.now[0])
        self.upstream.play_requests = self.upstream.dav_requests = 0
        self.upstream.viewed = {}
        self.upstream.ignore_range = self.upstream.truncate = False
        self.server = module.make_server(self.cache, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.origin = 'http://127.0.0.1:%d' % self.server.server_port
        self.catalog = self.cache.catalog(HASH)
        self.files = self.catalog['files']
        self.first = next(f for f in self.files if f['id'] == 1)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def request(self, path, payload=None, headers=None, method=None):
        request_headers = {'Authorization': 'Bearer ' + TOKEN}
        request_headers.update(headers or {})
        body = json.dumps(payload).encode() if payload is not None else None
        try:
            return opener.open(Request(self.origin + path, body, request_headers, method=method), timeout=3)
        except HTTPError as error:
            error.close()
            raise

    def event(self, event='heartbeat', index=1, position=0, duration=100, client='iina'):
        self.cache.event({'hash': HASH, 'index': index, 'event': event,
                          'client': client, 'position': position, 'duration': duration})

    def fill(self, index=1, count=None):
        target = self.cache.path(HASH, index, create=True)
        target.write_bytes(DATA[index] if count is None else DATA[index][:count])
        return target

    def test_download_all_files_without_viewed_side_effect(self):
        while True:
            task = self.cache.choose_download()
            if not task:
                break
            self.cache.download_one(*task)
        for file in self.files:
            self.assertEqual(self.cache.path(HASH, file['id']).read_bytes(), DATA[file['id']])
        self.assertEqual(self.upstream.play_requests, 0)
        self.assertGreater(self.upstream.dav_requests, 0)
        self.assertFalse(any(self.cache.watched(self.catalog, f) for f in self.files))

    def test_missing_and_explicit_false_flags_preserve_existing_cache(self):
        self.fill(count=13000)
        self.event('end', position=100)
        self.now[0] += 604801
        for explicit in (False, True):
            config = {k: v for k, v in self.config.items() if k not in
                      ('download_enabled', 'cleanup_watched', 'cleanup_expired')}
            if explicit:
                example = json.loads((Path(__file__).parents[1] / 'companion.example.json').read_text())
                config.update({k: example[k] for k in
                               ('download_enabled', 'cleanup_watched', 'cleanup_expired')})
            restarted = module.Companion(config, clock=lambda: self.now[0] - 121)
            restarted.clock = lambda: self.now[0]
            self.assertIsNone(restarted.choose_download())
            restarted.download_one(HASH, self.first)
            self.assertEqual(restarted.cleanup(), [])
            self.assertEqual(restarted.path(HASH, 1).read_bytes(), DATA[1][:13000])
            self.assertEqual(self.upstream.dav_requests, 0)
            self.assertFalse(any(restarted.status()['features'].values()))

    def test_cleanup_rules_are_independent(self):
        first = self.fill()
        second = self.fill(index=2)
        self.event('end', position=95)
        self.now[0] += 604801
        self.cache.cleanup_expired = False
        self.assertEqual(self.cache.cleanup(), [(HASH, 1)])
        self.assertTrue(second.exists())
        self.assertIsNotNone(self.cache.choose_download())
        self.assertFalse(self.catalog['expired'])
        # Watched files remain when only age-based cleanup is enabled.
        first = self.fill()
        self.cache.cleanup_watched = False
        self.cache.cleanup_expired = True
        self.catalog['last_use'] = self.now[0]
        self.assertEqual(self.cache.cleanup(), [])
        self.now[0] += 604800
        removed = self.cache.cleanup()
        self.assertIn((HASH, 1), removed)
        self.assertIn((HASH, 2), removed)
        self.assertFalse(first.exists() or second.exists())

    def test_disabled_download_keeps_explicit_cleanup_working(self):
        target = self.fill()
        self.event('end', position=95)
        self.now[0] += 121
        self.cache.download_enabled = False
        self.assertIsNone(self.cache.choose_download())
        self.cache.download_one(HASH, self.first)
        self.assertEqual(self.upstream.dav_requests, 0)
        self.assertEqual(self.cache.cleanup(), [(HASH, 1)])
        self.assertFalse(target.exists())

    def test_feature_flags_reject_non_boolean_values(self):
        for name in ('download_enabled', 'cleanup_watched', 'cleanup_expired'):
            for value in ('false', 'true', 0, 1, None):
                with self.assertRaisesRegex(ValueError, name):
                    module.Companion(dict(self.config, **{name: value}))

    def test_fresh_installer_creates_disabled_server_features(self):
        home = Path(self.temp.name) / 'fresh-home'
        args = module.argparse.Namespace(server='http://127.0.0.1:8090', bind='127.0.0.1', port=8092)
        # Stop at validation, before any process/cron changes; inspect the real generated file.
        with patch.object(module.Path, 'home', return_value=home), \
                patch.object(module, 'Companion', side_effect=RuntimeError('before startup')):
            with self.assertRaisesRegex(RuntimeError, 'before startup'):
                module.install(args)
        path = home / 'Library/Application Support/iina-torrserver/companion.json'
        config = json.loads(path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        for name in ('download_enabled', 'cleanup_watched', 'cleanup_expired'):
            self.assertIs(config[name], False)

    def test_resume_after_restart(self):
        self.fill(count=13000)
        restarted = module.Companion(self.config, clock=lambda: self.now[0])
        restarted.download_one(HASH, self.first)
        self.assertEqual(restarted.path(HASH, 1).read_bytes(), DATA[1])

    def test_partial_range_crosses_cached_prefix(self):
        self.fill(count=6000)
        with self.request('/media/' + HASH + '/1/movie.mkv', headers={'Range': 'bytes=5000-9000'}) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers['Content-Range'], 'bytes 5000-9000/%d' % len(DATA[1]))
            self.assertEqual(response.read(), DATA[1][5000:9001])
        self.assertEqual(self.upstream.play_requests, 1)

    def test_seek_beyond_downloaded_prefix(self):
        self.fill(count=100)
        with self.request('/media/' + HASH + '/1/movie.mkv', headers={'Range': 'bytes=-257'}) as response:
            self.assertEqual(response.read(), DATA[1][-257:])

    def test_completed_file_works_without_upstream(self):
        self.fill()
        self.cache.origin = 'http://127.0.0.1:1'
        with self.request('/media/' + HASH + '/1/movie.mkv') as response:
            self.assertEqual(response.read(), DATA[1])
        self.assertEqual(self.upstream.play_requests, 0)

    def test_head_does_not_mark_viewed_or_fetch_content(self):
        with self.request('/media/' + HASH + '/1/movie.mkv', method='HEAD') as response:
            self.assertEqual(response.headers['Content-Length'], str(len(DATA[1])))
            self.assertEqual(response.read(), b'')
        self.assertEqual(self.upstream.play_requests, 0)

    def test_invalid_ranges(self):
        for value in ['bytes=999999-', 'bytes=4-2', 'bytes=0-1,4-5', 'bytes=-0', 'nonsense']:
            with self.assertRaises(HTTPError) as raised:
                self.request('/media/' + HASH + '/1/movie.mkv', headers={'Range': value})
            self.assertEqual(raised.exception.code, 416)

    def test_95_percent_only_after_player_stops(self):
        target = self.fill()
        self.event(position=94.99)
        self.now[0] += 121
        self.assertEqual(self.cache.cleanup(), [])
        self.event(position=95)
        self.assertEqual(self.cache.cleanup(), [])
        self.assertTrue(target.exists())
        self.event('end', position=95)
        self.assertEqual(self.cache.cleanup(), [(HASH, 1)])
        self.assertFalse(target.exists())
        self.assertNotEqual(self.cache.choose_download()[1]['id'], 1)

    def test_eof_and_progress_survive_restart(self):
        self.event('end', position=100)
        restarted = module.Companion(self.config, clock=lambda: self.now[0])
        self.assertTrue(restarted.watched(restarted.state[HASH], self.first))

    def test_week_expiry_does_not_refill_until_new_use(self):
        target = self.fill()
        self.now[0] += 604799
        self.assertEqual(self.cache.cleanup(), [])
        self.now[0] += 1
        self.assertEqual(self.cache.cleanup(), [(HASH, 1)])
        self.assertIsNone(self.cache.choose_download())
        self.assertFalse(target.exists())
        self.event('start')
        self.assertIsNotNone(self.cache.choose_download())

    def test_pause_heartbeat_renews_retention(self):
        self.fill()
        self.now[0] += 604790
        self.event(position=10)
        self.now[0] += 604790
        self.assertEqual(self.cache.cleanup(), [])

    def test_cleanup_protects_http_reader_and_downloader(self):
        self.fill()
        self.event('end', position=100)
        self.now[0] += 121
        self.cache.readers[HASH] = 1
        self.assertEqual(self.cache.cleanup(), [])
        self.cache.readers[HASH] = 0
        self.cache.downloading = (HASH, 1)
        self.assertEqual(self.cache.cleanup(), [])
        self.cache.downloading = None
        self.assertEqual(self.cache.cleanup(), [(HASH, 1)])

    def test_second_player_prevents_cleanup(self):
        self.fill()
        self.event(position=100, client='one')
        self.event(index=2, client='two')
        self.event('end', position=100, client='one')
        self.now[0] += 119
        self.assertEqual(self.cache.cleanup(), [])
        self.now[0] += 2
        self.assertEqual(self.cache.cleanup(), [(HASH, 1)])

    def test_restart_grace_prevents_deleting_paused_player_cache(self):
        self.fill()
        self.event('end', position=100)
        self.now[0] += 121
        restarted = module.Companion(self.config, clock=lambda: self.now[0])
        self.assertEqual(restarted.cleanup(), [])
        self.now[0] += 121
        self.assertEqual(restarted.cleanup(), [(HASH, 1)])

    def test_quota_and_free_disk_stop_only_background_download(self):
        self.cache.max_bytes = 100
        self.cache.download_one(HASH, self.first)
        self.assertEqual(self.cache.available(HASH, self.first), 0)
        with self.request('/media/' + HASH + '/1/movie.mkv', headers={'Range': 'bytes=0-99'}) as response:
            self.assertEqual(response.read(), DATA[1][:100])
        self.cache.max_bytes = 10000000
        self.cache.reserve = 10**18
        self.cache.download_one(HASH, self.first)
        self.assertEqual(self.cache.available(HASH, self.first), 0)

    def test_upstream_range_ignored_does_not_corrupt_prefix(self):
        self.fill(count=100)
        self.upstream.ignore_range = True
        with self.assertRaises(ValueError):
            self.cache.download_one(HASH, self.first)
        self.assertEqual(self.cache.available(HASH, self.first), 100)
        self.assertIsNone(self.cache.downloading)

    def test_truncated_download_does_not_append_bad_chunk(self):
        self.fill(count=100)
        self.upstream.truncate = True
        with self.assertRaises(Exception):
            self.cache.download_one(HASH, self.first)
        self.assertEqual(self.cache.available(HASH, self.first), 100)

    def test_auth_and_browser_origin(self):
        for headers in [{'Authorization': ''}, {'Authorization': 'Bearer wrong'}, {'Origin': 'https://bad.example'}]:
            with self.assertRaises(HTTPError) as raised:
                self.request('/event', {'event': 'start'}, headers=headers)
            self.assertEqual(raised.exception.code, 403)
        self.assertNotIn(TOKEN, json.dumps(self.cache.status()))

    def test_malformed_events_and_traversal_rejected(self):
        for payload in [{'hash': '../bad'}, {'hash': HASH, 'index': 1, 'client': 'test',
                         'event': 'heartbeat', 'position': float('nan')}]:
            with self.assertRaises(ValueError):
                self.cache.event(payload)
        for path in ['/media/../1/file', '/media/' + HASH + '/1/../../outside']:
            with self.assertRaises(HTTPError) as raised:
                self.request(path)
            self.assertEqual(raised.exception.code, 404)

    def test_symlink_protection_and_unknown_files_preserved(self):
        outside = Path(self.temp.name) / 'outside'
        outside.write_bytes(b'keep')
        self.cache.path(HASH, 1, create=True).symlink_to(outside)
        with self.assertRaises(ValueError):
            self.cache.available(HASH, self.first)
        self.assertEqual(outside.read_bytes(), b'keep')
        self.cache.path(HASH, 2, create=True).parent.joinpath('unrelated.txt').write_text('keep')
        with self.assertRaises(ValueError):
            self.cache.path('../bad', 1)

    def test_corrupt_state_stops_instead_of_deleting(self):
        target = self.fill()
        self.cache.state_path.write_text('broken')
        with self.assertRaises(ValueError):
            module.Companion(self.config)
        self.assertTrue(target.exists())

    def test_nonempty_unmarked_directory_is_not_adopted(self):
        root = Path(self.temp.name) / 'other'
        root.mkdir()
        (root / 'personal-file').write_text('keep')
        with self.assertRaises(ValueError):
            module.Companion(dict(self.config, cache_dir=str(root)))

    def test_installer_matches_real_macos_python_and_preserves_other_processes(self):
        target = '/Users/test/Library/Application Support/iina-torrserver/companion.py'
        config = '/Users/test/Library/Application Support/iina-torrserver/companion.json'
        suffix = ' ' + target + ' --config ' + config
        self.assertTrue(module.service_command_matches('/Library/Developer/Python.app/Contents/MacOS/Python' + suffix, target, config))
        self.assertTrue(module.service_command_matches('/usr/bin/python3' + suffix, target, config))
        self.assertFalse(module.service_command_matches('/bin/bash' + suffix, target, config))
        self.assertFalse(module.service_command_matches('/usr/bin/python3' + suffix + '.other', target, config))

    def test_offline_progress_retry_survives_restart(self):
        self.cache.event({'hash': HASH, 'index': 1, 'client': 'offline', 'event': 'end',
                          'position': 90, 'duration': 100, 'sync': True})
        restarted = module.Companion(self.config)
        origin = restarted.origin
        restarted.origin = 'http://127.0.0.1:1'
        with self.assertRaises(Exception):
            restarted.sync_progress()
        self.assertTrue(restarted.state[HASH]['progress']['1']['pending'])
        restarted.origin = origin
        restarted.sync_progress()
        self.assertEqual(self.upstream.viewed[1], 90)
        self.assertFalse(restarted.state[HASH]['progress']['1']['pending'])

    def test_zero_start_does_not_erase_offline_progress(self):
        self.cache.event({'hash': HASH, 'index': 1, 'client': 'offline', 'event': 'end',
                          'position': 95, 'duration': 100, 'sync': True})
        self.event('start', duration=0)
        progress = self.cache.state[HASH]['progress']['1']
        self.assertTrue(progress['watched'] and progress['pending'])
        self.assertEqual(progress['position'], 95)

    def test_related_subtitles_follow_their_episode_only(self):
        for index in (1, 2, 3):
            self.fill(index)
        self.event('end', position=100)
        self.now[0] += 121
        self.assertEqual(self.cache.cleanup(), [(HASH, 1)])
        self.assertTrue(self.cache.path(HASH, 3).exists())
        self.event('end', index=2, position=95)
        self.assertEqual(set(self.cache.cleanup()), {(HASH, 2), (HASH, 3)})
        self.assertIsNone(self.cache.choose_download())

    def test_service_singleton_and_clean_restart(self):
        config = dict(self.config, cache_dir=self.temp.name + '/service-cache', bind='127.0.0.1', port=0)
        config_path = Path(self.temp.name) / 'service.json'
        config_path.write_text(json.dumps(config))
        command = [sys.executable, str(Path(__file__).parents[1] / 'companion.py'), '--config', str(config_path)]
        lock = Path(config['cache_dir']) / '.service-lock'
        process = None
        try:
            for _ in range(2):
                process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                for _ in range(100):
                    if lock.exists() and lock.read_text() == str(process.pid):
                        break
                    time.sleep(.02)
                else:
                    self.fail('Service did not acquire its process lock')
                second = subprocess.run(command, capture_output=True, timeout=5)
                self.assertEqual(second.returncode, 0, second.stderr.decode())
                self.assertIsNone(process.poll())
                process.terminate()
                _, error = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, error.decode())
                process = None
            self.assertEqual(json.loads((Path(config['cache_dir']) / 'state.json').read_text()), {})
        finally:
            if process and process.poll() is None:
                process.terminate()
                process.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()

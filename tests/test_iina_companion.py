"""Actual IINA/mpv HTTP playback with cache enabled; all servers/data are isolated fixtures."""
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

spec = importlib.util.spec_from_file_location('companion', Path(__file__).parents[1] / 'companion.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
HASH = 'b' * 40
TOKEN = 'isolated-IINA-test-token-' * 3


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, value):
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.server.offline:
            self.send_error(503)
            return
        if self.path == '/viewed':
            self.server.viewed[data['file_index']] = data['timecode']
            return self.reply({})
        self.reply([{'hash': HASH, 'title': 'IINA fixture', 'category': 'other'}])

    def do_GET(self):
        if self.server.offline:
            self.send_error(503)
            return
        query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        if 'stat' in query:
            return self.reply({'hash': HASH, 'title': 'IINA fixture', 'category': 'other',
                'file_stats': [{'id': i, 'path': 'Fixture/' + name, 'length': len(self.server.data)}
                               for i, name in [(1, 'S01E1 — тест.mkv'), (2, 'S01E2.mkv')]]})
        body = self.server.data
        first, last = module.byte_range(self.headers.get('Range'), len(body))
        self.send_response(200 if self.server.ignore_range or not self.headers.get('Range') else 206)
        self.send_header('Content-Length', str(last - first + 1))
        if not self.server.ignore_range:
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (first, last, len(body)))
        self.end_headers()
        self.wfile.write(body[first:last + 1])


class CountingHandler(module.Handler):
    requests = 0

    def handle_one_request(self):
        type(self).requests += 1
        super().handle_one_request()


def main():
    with tempfile.TemporaryDirectory(prefix='iina-cache-engine-') as directory:
        root = Path(directory)
        video = root / 'fixture.mkv'
        environment = dict(os.environ)
        environment.pop('DYLD_LIBRARY_PATH', None)
        subprocess.run(['/opt/homebrew/bin/ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
            'color=c=black:s=64x64:r=10', '-f', 'lavfi', '-i', 'sine=frequency=440',
            '-f', 'lavfi', '-i', 'sine=frequency=220', '-map', '0:v', '-map', '1:a', '-map', '2:a',
            '-metadata:s:a:0', 'title=Original', '-metadata:s:a:0', 'language=eng',
            '-metadata:s:a:1', 'title=LostFilm', '-metadata:s:a:1', 'language=rus',
            '-t', '18', '-c:v', 'libx264', '-c:a', 'aac', str(video)], check=True, env=environment)
        upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        upstream.daemon_threads = True
        upstream.data, upstream.viewed, upstream.ignore_range, upstream.offline = video.read_bytes(), {}, False, False
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        origin = 'http://127.0.0.1:%d' % upstream.server_port
        cache = module.Companion({'torrserver': origin, 'token': TOKEN, 'cache_dir': str(root / 'cache'),
                                  'download_enabled': True, 'cleanup_watched': True, 'cleanup_expired': True,
                                  'min_free_bytes': 0, 'lease_seconds': 1})
        catalog = cache.catalog(HASH)
        first = catalog['files'][0]
        cache.path(HASH, 1, create=True).write_bytes(upstream.data[:1000])
        cache.started -= 2
        server = module.make_server(cache, port=0)
        server.RequestHandlerClass = CountingHandler
        threading.Thread(target=server.serve_forever, daemon=True).start()
        companion_url = 'http://127.0.0.1:%d' % server.server_port
        library = ctypes.CDLL('/Applications/IINA.app/Contents/Frameworks/libmpv.2.dylib')
        library.mpv_create.restype = ctypes.c_void_p
        library.mpv_set_option_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
        library.mpv_initialize.argtypes = [ctypes.c_void_p]
        library.mpv_command.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_char_p)]
        library.mpv_get_property_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        library.mpv_get_property_string.restype = ctypes.c_void_p
        library.mpv_free.argtypes = [ctypes.c_void_p]
        library.mpv_terminate_destroy.argtypes = [ctypes.c_void_p]
        handle = library.mpv_create()

        def command(*values):
            args = (ctypes.c_char_p * (len(values) + 1))(*[v.encode() for v in values], None)
            assert library.mpv_command(handle, args) >= 0

        def prop(name):
            pointer = library.mpv_get_property_string(handle, name.encode())
            if not pointer:
                return None
            try:
                return ctypes.string_at(pointer).decode()
            finally:
                library.mpv_free(pointer)

        def wait(condition, message):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if condition():
                    return
                time.sleep(.05)
            raise AssertionError(message)

        try:
            script = Path(__file__).parents[1] / 'torrserver-progress.lua'
            config_dir = root / 'iina-config'
            (config_dir / 'script-opts').mkdir(parents=True)
            configuration = config_dir / 'script-opts/torrserver-progress.conf'
            base_config = ('servers=' + origin + '\ncompanion_url=' + companion_url
                           + '\ncompanion_token=' + TOKEN + '\naudio_memory=' + str(root / 'audio.json') + '\n')
            original = origin + '/stream/video.mkv?link=' + HASH + '&index=1&play'
            # Read actual .conf files through mp.options, including shipping defaults.
            for extra in ('', 'playlist=no\naudio_memory_enabled=no\nprogress=no\n',
                          'server_cache=yes\nprogress=no\n'):
                configuration.write_text(base_config + extra)
                if extra.startswith('playlist='):
                    memory = {HASH: {'title': 'original', 'lang': 'en'}}
                    (root / 'audio.json').write_text(json.dumps(memory))
                for key, value in {'config': 'yes', 'config-dir': str(config_dir), 'vo': 'null',
                                   'ao': 'null', 'idle': 'yes', 'keep-open': 'always', 'aid': '2',
                                   'terminal': 'yes', 'msg-level': 'all=warn', 'scripts': str(script)}.items():
                    assert library.mpv_set_option_string(handle, key.encode(), value.encode()) >= 0
                assert library.mpv_initialize(handle) >= 0
                upstream.viewed.clear()
                before_requests = CountingHandler.requests
                command('loadfile', original)
                wait(lambda: float(prop('time-pos') or 0) > .2, 'Feature-switch fixture did not play')
                command('set', 'pause', 'yes')
                if not extra:
                    assert len(json.loads(prop('playlist'))) == 2
                    command('set', 'aid', '2')
                    wait(lambda: (root / 'audio.json').exists(), 'Default audio memory is disabled')
                    wait(lambda: upstream.viewed.get(1, 0) > 0, 'Default progress is disabled')
                    assert CountingHandler.requests == before_requests, 'Credentials alone enabled cache'
                    print('PASS: default .conf enables IINA improvements without contacting server companion', flush=True)
                elif extra.startswith('playlist='):
                    assert len(json.loads(prop('playlist'))) == 1
                    assert prop('aid') == '2', 'Disabled audio memory changed IINA audio selection'
                    command('set', 'aid', '1')
                    time.sleep(.2)
                    assert json.loads((root / 'audio.json').read_text()) == memory
                    assert CountingHandler.requests == before_requests
                else:
                    assert prop('stream-open-filename').startswith(companion_url)
                command('stop')
                library.mpv_terminate_destroy(handle)
                handle = None
                if extra:
                    assert not upstream.viewed, 'Disabled progress still wrote to TorrServer'
                    assert not any(p.get('pending') for p in cache.state[HASH].get('progress', {}).values())
                    print('PASS: configuration switches disable requested features, including offline progress', flush=True)
                handle = library.mpv_create()
            configuration.unlink()
            options = {'config': 'no', 'vo': 'null', 'ao': 'null', 'idle': 'yes', 'keep-open': 'always',
                       'terminal': 'yes', 'msg-level': 'all=warn', 'scripts': str(script),
                       'script-opts': 'torrserver-progress-servers=' + origin + ',torrserver-progress-companion_url='
                       + companion_url + ',torrserver-progress-companion_token=' + TOKEN
                       + ',torrserver-progress-server_cache=yes'
                       + ',torrserver-progress-audio_memory=' + str(root / 'audio.json')}
            for key, value in options.items():
                assert library.mpv_set_option_string(handle, key.encode(), value.encode()) >= 0
            assert library.mpv_initialize(handle) >= 0
            command('loadfile', original)
            wait(lambda: float(prop('time-pos') or 0) > .2, 'Cached-prefix playback did not start')
            assert prop('path') == original
            assert prop('stream-open-filename').startswith(companion_url)
            assert [p['title'] for p in json.loads(prop('playlist'))] == ['S01E1 — тест.mkv', 'S01E2.mkv']
            assert cache.active(HASH)
            print('PASS: IINA uses authenticated companion, original identity and episode names retained', flush=True)
            duration = float(prop('duration'))
            command('set', 'pause', 'yes')
            command('seek', str(duration * .96), 'absolute+exact')
            wait(lambda: float(prop('time-pos') or 0) >= duration * .95, 'Seek into uncached part failed')
            seek_position = float(prop('time-pos'))
            command('set', 'pause', 'no')
            # Let playback actually resume; back-to-back pause commands can be coalesced
            # before Lua receives either property notification.
            wait(lambda: float(prop('time-pos') or 0) > seek_position + .1,
                 'Playback did not resume after the seek')
            command('set', 'pause', 'yes')
            wait(lambda: cache.watched(catalog, first), 'IINA did not report the 95% threshold')
            assert cache.cleanup() == []
            assert upstream.viewed[1] >= duration * .95
            command('stop')
            wait(lambda: not cache.active(HASH), 'IINA stop did not release its cache lease')
            assert cache.cleanup() == [(HASH, 1)]
            print('PASS: real IINA seek/pause/95%/stop -> progress and safe per-file cleanup', flush=True)
            # A clean helper error before playback must retry the original stream once.
            upstream.ignore_range = True
            command('set', 'pause', 'no')
            command('loadfile', original)
            wait(lambda: prop('stream-open-filename') == original and float(prop('time-pos') or 0) > .1,
                 'Companion media failure did not fall back to original TorrServer playback')
            print('PASS: unavailable companion content falls back to original stream', flush=True)
            command('stop')
            cache.path(HASH, 1, create=True).write_bytes(upstream.data)
            upstream.offline = True
            command('loadfile', original)
            wait(lambda: prop('stream-open-filename') and prop('stream-open-filename').startswith(companion_url)
                 and float(prop('time-pos') or 0) > .1, 'Complete cache did not play with TorrServer unavailable')
            print('PASS: real IINA plays complete cache while original TorrServer is unavailable', flush=True)
            command('stop')
            wait(lambda: cache.state[HASH]['progress'].get('1', {}).get('pending'),
                 'Offline IINA progress was not queued')
            upstream.offline = False
            cache.sync_progress()
            assert not cache.state[HASH]['progress']['1']['pending']
            assert upstream.viewed[1] == cache.state[HASH]['progress']['1']['position']
            print('PASS: offline IINA progress is retried when TorrServer returns', flush=True)
        finally:
            if handle:
                library.mpv_terminate_destroy(handle)
            for service in (server, upstream):
                service.shutdown()
                service.server_close()


if __name__ == '__main__':
    main()

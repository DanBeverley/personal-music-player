"""Loopback-only, access-code protected gateway for temporary HTTPS previews."""
import argparse
import functools
import hmac
import http.client
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
from urllib.parse import parse_qs, unquote, urlsplit

BUILD = Path(__file__).resolve().parents[1] / 'app/build/web'
LOGIN = b'''<!doctype html><meta name="viewport" content="width=device-width">
<title>Neatie preview</title><h1>Neatie preview</h1>
<form method="post" action="/preview-login"><label>Access code
<input name="code" type="password" required></label><button>Open preview</button></form>'''


class Gateway(SimpleHTTPRequestHandler):
    def __init__(self, *args, code, session, backend_port, public_access=False, **kwargs):
        self.public_access = public_access
        self.code, self.session, self.backend_port = code, session, backend_port
        super().__init__(*args, directory=str(BUILD), **kwargs)

    def log_message(self, *args):
        pass  # Never log cookies, access codes, or provider stream parameters.

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        super().end_headers()

    def authorized(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
            value = cookie.get('neatie_preview')
            return value is not None and hmac.compare_digest(value.value, self.session)
        except Exception:
            return False

    def gate(self):
        if self.public_access or self.authorized():
            return True
        self.send_response(401)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(LOGIN)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(LOGIN)
        return False

    def body(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            length = -1
        if not 0 <= length <= 65536 or self.headers.get('Transfer-Encoding'):
            self.send_error(413)
            return None
        return self.rfile.read(length)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == '/preview-login':
            body = self.body()
            if body is None:
                return
            code = parse_qs(body.decode('utf-8', errors='replace')).get('code', [''])[0]
            if not hmac.compare_digest(code.encode(), self.code.encode()):
                self.send_error(403)
                return
            self.send_response(303)
            self.send_header('Set-Cookie', f'neatie_preview={self.session}; Path=/; Secure; HttpOnly; SameSite=Strict')
            self.send_header('Location', '/')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if not self.gate():
            return
        if path not in ('/search', '/suggest', '/search_interaction', '/related_tracks', '/playback/resolve', '/prepare_session', '/recommend'):
            self.send_error(404)
            return
        body = self.body()
        if body is not None:
            self.proxy(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.gate():
            return
        path = unquote(urlsplit(self.path).path)
        if '\\' in path or any(part in ('.', '..') for part in path.split('/')):
            self.send_error(404)
            return
        if path in ('/recent_search_picks', '/charts', '/new_releases') or any(path.startswith(prefix) and path[len(prefix):] and '/' not in path[len(prefix):]
               for prefix in ('/playback/stream/', '/artist_artwork/', '/entity_artwork/', '/album/', '/artist/', '/lyrics/')):
            self.proxy()
            return
        target = (BUILD / path.lstrip('/')).resolve()
        if not target.is_relative_to(BUILD.resolve()) or any(p.startswith('.') for p in path.split('/') if p):
            self.send_error(404)
            return
        if target.is_dir() and not (target / 'index.html').is_file():
            self.send_error(404)
            return
        if self.command == 'HEAD':
            super().do_HEAD()
        else:
            super().do_GET()

    def proxy(self, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.backend_port, timeout=90)
        sent = False
        try:
            headers = {k: v for k, v in self.headers.items() if k.lower() in ('content-type', 'range', 'authorization')}
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() in ('content-type', 'content-length', 'content-range', 'accept-ranges'):
                    self.send_header(key, value)
            self.end_headers()
            sent = True
            if self.command != 'HEAD':
                while chunk := response.read(65536):
                    self.wfile.write(chunk)
        except (OSError, http.client.HTTPException):
            if not sent:
                self.send_error(502, 'Music backend unavailable')
        finally:
            connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8081)
    parser.add_argument('--backend-port', type=int, default=8000)
    parser.add_argument('--public', action='store_true', help='Allow access without a preview code.')
    args = parser.parse_args()
    if not (BUILD / 'main.dart.js').is_file():
        parser.error('Build app web release first; app/build/web/main.dart.js is missing.')
    code = secrets.token_urlsafe(12)
    handler = functools.partial(Gateway, code=code, session=secrets.token_urlsafe(32), backend_port=args.backend_port, public_access=args.public)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler)
    print(f'Preview: http://127.0.0.1:{args.port}' + ('' if args.public else f'; access code: {code}'), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()

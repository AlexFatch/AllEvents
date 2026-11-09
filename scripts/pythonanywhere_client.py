"""Private local client for inspecting an existing PythonAnywhere deployment."""
import ast
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parents[1]


class PythonAnywhere:
    def __init__(self):
        path = ROOT / '.pythonanywhere.json'
        if not path.exists():
            raise SystemExit('Run python3 scripts/configure_pythonanywhere.py in Terminal first.')
        self.config = json.loads(path.read_text())
        if self.config['api_host'] not in {'www.pythonanywhere.com', 'eu.pythonanywhere.com'}:
            raise ValueError('Unsupported API host')
        self.base = 'https://' + self.config['api_host'] + '/api/v0/user/' + quote(self.config['username'], safe='')
        self.session = requests.Session()
        self.session.headers['Authorization'] = 'Token ' + self.config['token']
        self.last_request = 0

    def request(self, method, endpoint, **kwargs):
        # The files endpoint has a 40/minute limit; use at most 37.5/minute.
        delay = 1.6 - (time.monotonic() - self.last_request)
        if delay > 0:
            time.sleep(delay)
        self.last_request = time.monotonic()
        result = self.session.request(method, self.base + endpoint, timeout=45,
                                      allow_redirects=False, **kwargs)
        if result.status_code >= 400 and result.status_code != 404:
            raise RuntimeError(f'PythonAnywhere API returned HTTP {result.status_code} for {method} request')
        return result

    def info(self):
        result = self.request('GET', '/webapps/' + self.config['domain'] + '/')
        result.raise_for_status()
        raw = result.json()
        return {key: raw.get(key) for key in ('domain_name', 'source_directory', 'virtualenv_path', 'python_version', 'wsgi_configuration_file')}

    def read(self, path):
        result = self.request('GET', '/files/path' + quote(path, safe='/'))
        if result.status_code == 404:
            return None
        result.raise_for_status()
        return result.content

    def write(self, path, content):
        result = self.request('POST', '/files/path' + quote(path, safe='/'),
                              files={'content': ('upload', content)})
        result.raise_for_status()

    def reload(self):
        result = self.request('POST', '/webapps/' + self.config['domain'] + '/reload/')
        result.raise_for_status()


def private_save(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(content)


if __name__ == '__main__':
    client = PythonAnywhere()
    info = client.info()
    folder = ROOT / '.deploy' / 'inspection'
    private_save(folder / 'webapp.json', json.dumps(info, indent=2).encode())
    print(json.dumps(info, indent=2))
    wsgi_path = info.get('wsgi_configuration_file') or '/var/www/anesu_pythonanywhere_com_wsgi.py'
    source = info.get('source_directory')
    if isinstance(wsgi_path, str):
        content = client.read(wsgi_path)
        if content is not None:
            private_save(folder / 'wsgi.py', content)
            print('Hosted WSGI configuration saved privately for review.')
            # This app's WSGI file explicitly prepends `path` to sys.path;
            # the Web tab's source_directory points at a different, older app.
            for node in ast.parse(content.decode()).body:
                if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str) and node.value.value.startswith('/home/')
                        and any(isinstance(target, ast.Name) and target.id == 'path' for target in node.targets)):
                    source = node.value.value
    if source:
        print('Effective application directory:', source)
        content = client.read(source.rstrip('/') + '/flask_app.py')
        if content is not None:
            private_save(folder / 'flask_app.py', content)
            print('Hosted app saved privately; SHA256:', hashlib.sha256(content).hexdigest())

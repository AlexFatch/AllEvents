"""Deploy the privately backed-up release, run its event import, and verify it."""
import ast
import hashlib
import json
from pathlib import Path
import requests
from pythonanywhere_client import PythonAnywhere, ROOT, private_save


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    client = PythonAnywhere()
    folder = Path((ROOT / '.deploy/current-release.txt').read_text())
    manifest = json.loads((folder / 'manifest.json').read_text())
    if manifest['domain'] != 'anesu.pythonanywhere.com' or manifest['remote_root'] != '/home/anesu':
        raise RuntimeError('Release target does not match the reviewed deployment')
    entries = [item for item in manifest['files'] if item['changed']]
    for item in entries:
        if sha((folder / 'after' / item['path']).read_bytes()) != item['after_sha']:
            raise RuntimeError('Staged file changed: ' + item['path'])
    ast.parse((folder / 'after/flask_app.py').read_text(), feature_version=(3, 10))
    app_entry = next(item for item in entries if item['path'] == 'flask_app.py')
    current = client.read('/home/anesu/flask_app.py')
    if current is None or sha(current) != app_entry['before_sha']:
        raise RuntimeError('The hosted app changed since review; inspect it again')
    wsgi_path = '/var/www/anesu_pythonanywhere_com_wsgi.py'
    wsgi = client.read(wsgi_path)
    if not wsgi:
        raise RuntimeError('Hosted WSGI file not found')
    inspected_wsgi = (ROOT / '.deploy/inspection/wsgi.py').read_bytes()
    if wsgi != inspected_wsgi:
        raise RuntimeError('The hosted WSGI configuration changed since review')
    private_save(folder / 'before/wsgi.py', wsgi)
    public = 'https://anesu.pythonanywhere.com'
    baseline = requests.get(public + '/status', timeout=45)
    if baseline.status_code != 200 or baseline.text.strip() != 'OK':
        raise RuntimeError('Hosted app did not pass its pre-deployment health check')

    # A temporary WSGI startup hook runs the reviewed, transactional, idempotent
    # data import. The exact original WSGI bytes are restored immediately after
    # verification. Existing records are never replaced.
    receipt_path = '/home/anesu/.eventsys-import-' + folder.name + '.json'
    hook = '''
# One-time reviewed event import for this deployment.
import importlib.util as _eventsys_util
import json as _eventsys_json
import os as _eventsys_os
_eventsys_spec = _eventsys_util.spec_from_file_location('_eventsys_importer', '/home/anesu/scripts/import_local_events.py')
_eventsys_module = _eventsys_util.module_from_spec(_eventsys_spec)
_eventsys_spec.loader.exec_module(_eventsys_module)
with open('/home/anesu/data/zimbabwe_events_2026.json') as _eventsys_file:
    _eventsys_records = _eventsys_json.load(_eventsys_file)
_eventsys_added = _eventsys_module.import_events('/home/anesu/brandit_tickets_v1.db', _eventsys_records)
try:
    _eventsys_fd = _eventsys_os.open(RECEIPT_PATH, _eventsys_os.O_WRONLY | _eventsys_os.O_CREAT | _eventsys_os.O_EXCL, 0o600)
except FileExistsError:
    _eventsys_fd = None
if _eventsys_fd is not None:
    with _eventsys_os.fdopen(_eventsys_fd, 'w') as _eventsys_receipt:
        _eventsys_json.dump({'added': _eventsys_added, 'reviewed': len(_eventsys_records)}, _eventsys_receipt)
'''.replace('RECEIPT_PATH', repr(receipt_path))
    migration_wsgi = wsgi + b'\n' + hook.encode()
    ast.parse(migration_wsgi.decode(), feature_version=(3, 10))
    applied = []
    wsgi_changed = False
    try:
        for item in entries:
            client.write('/home/anesu/' + item['path'], (folder / 'after' / item['path']).read_bytes())
            applied.append(item)
            print('Uploaded ' + item['path'], flush=True)
        client.write(wsgi_path, migration_wsgi)
        wsgi_changed = True
        print('Reloading updated app and running the one-time event import...', flush=True)
        client.reload()
        homepage = requests.get(public + '/', timeout=60)
        if homepage.status_code != 200 or 'id="heroSlideshow"' not in homepage.text:
            raise RuntimeError('Slideshow homepage failed live verification')
        if 'External listing' not in homepage.text or 'CIMAM Annual Conference 2026' not in homepage.text:
            raise RuntimeError('Imported listings are missing from the live homepage')
        receipt = client.read(receipt_path)
        if receipt is None:
            raise RuntimeError('Import receipt is missing')
        import_result = json.loads(receipt)
        private_save(folder / 'import-result.json', receipt)
        print('Event import:', json.dumps(import_result), flush=True)
        client.write(wsgi_path, wsgi)
        wsgi_changed = False
        client.reload()
        checks = [
            ('/', 'id="heroSlideshow"'),
            ('/buy/6', 'id="checkoutForm"'),
            ('/static/js/hero-slideshow.js', '6000'),
            ('/static/js/checkout.js', 'data'),
            ('/static/html5-qrcode.min.js', 'Html5Qrcode'),
            ('/static/event-art/music.svg', '<svg'),
            ('/static/uploads/7907ba4ae1584aac9801bc616493ac89.jpg', None),
        ]
        for path, marker in checks:
            result = requests.get(public + path, timeout=45)
            if result.status_code != 200 or (marker and marker not in result.text):
                raise RuntimeError('Live check failed for ' + path)
            print('Verified ' + path, flush=True)
        for path in ['/scanner', '/admin/users']:
            result = requests.get(public + path, timeout=45, allow_redirects=False)
            if result.status_code != 302 or '/login' not in result.headers.get('Location', ''):
                raise RuntimeError('Staff login guard failed for ' + path)
            print('Verified login guard ' + path, flush=True)
        if client.read(wsgi_path) != wsgi:
            raise RuntimeError('Original WSGI configuration was not restored')
        private_save(folder / 'deployed.json', json.dumps({'domain': manifest['domain'], 'files': len(applied), 'import': import_result, 'verified': True}).encode())
        print('Deployment completed and verified. Original WSGI configuration restored.', flush=True)
    except BaseException:
        print('Deployment failed; restoring backed-up application files. Database records are preserved.', flush=True)
        for item in reversed(applied):
            if item['existed']:
                client.write('/home/anesu/' + item['path'], (folder / 'before' / item['path']).read_bytes())
        if wsgi_changed:
            client.write(wsgi_path, wsgi)
        client.reload()
        raise


if __name__ == '__main__':
    main()

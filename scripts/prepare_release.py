"""Stage application files for review before a PythonAnywhere update."""
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / 'dist' / 'pythonanywhere-update'
FILES = [ROOT / 'flask_app.py', ROOT / 'requirements.txt',
         ROOT / 'static/html5-qrcode.min.js', ROOT / 'static/logo.png',
         ROOT / 'data/zimbabwe_events_2026.json', ROOT / 'data/README.md',
         ROOT / 'scripts/import_local_events.py']
FILES += sorted((ROOT / 'templates').glob('*.html'))
FILES += sorted((ROOT / 'static/js').glob('*.js'))
FILES += sorted((ROOT / 'static/event-art').glob('*.svg'))
FILES += [ROOT / 'static/uploads' / name for name in (
    'audience-1853662_1280.jpg',
    '7907ba4ae1584aac9801bc616493ac89.jpg',
    '0a1afd8f67434fd4b5cd06d68c371208.jpeg')]


def prepare():
    entries = []
    for source in FILES:
        relative = source.relative_to(ROOT)
        destination = DEST / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        entries.append(dict(path=relative.as_posix(), sha256=hashlib.sha256(source.read_bytes()).hexdigest()))
    manifest = dict(domain='anesu.pythonanywhere.com', files=entries)
    (DEST / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'Staged {len(entries)} application files in {DEST}')
    print('Databases, credentials, local virtualenvs and WSGI configuration are excluded.')


if __name__ == '__main__':
    prepare()

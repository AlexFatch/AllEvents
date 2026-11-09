"""Run in Terminal to save the API token locally without displaying it."""
import getpass
import json
import os
from pathlib import Path

path = Path(__file__).resolve().parents[1] / '.pythonanywhere.json'
token = getpass.getpass('PythonAnywhere API token (hidden): ').strip()
if not token:
    raise SystemExit('No token entered; configuration was not changed.')
config = dict(username='anesu', domain='anesu.pythonanywhere.com',
              api_host='www.pythonanywhere.com', token=token)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.fchmod(fd, 0o600)
with os.fdopen(fd, 'w') as output:
    json.dump(config, output)
    output.write('\n')
print('PythonAnywhere credentials saved locally. The token was not displayed.')

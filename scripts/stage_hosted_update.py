"""Back up the exact files involved in this reviewed update; no remote writes."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from pythonanywhere_client import PythonAnywhere, ROOT, private_save

client=PythonAnywhere()
remote_root='/home/anesu'
paths=['static/js/hero-slideshow.js','static/js/checkout.js','static/html5-qrcode.min.js']
paths += [p.relative_to(ROOT).as_posix() for p in sorted((ROOT/'static/event-art').glob('*.svg'))]
paths += ['static/uploads/'+name for name in ('audience-1853662_1280.jpg','7907ba4ae1584aac9801bc616493ac89.jpg','0a1afd8f67434fd4b5cd06d68c371208.jpeg')]
paths += ['templates/'+name for name in ('base.html','landing.html','buy_ticket.html','admin_user.html','scanner.html','admin_events.html','admin_edit_event.html','select_event.html','admin_base.html')]
paths += ['data/zimbabwe_events_2026.json','data/README.md','scripts/import_local_events.py','flask_app.py']
folder=ROOT/'.deploy'/'releases'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
folder.mkdir(parents=True,mode=0o700)
entries=[]
for path in paths:
 before=client.read(remote_root+'/'+path)
 local=(ROOT/path).read_bytes()
 changed=before!=local
 if before is not None:
  private_save(folder/'before'/path,before)
 private_save(folder/'after'/path,local)
 entries.append(dict(path=path,changed=changed,existed=before is not None,
                     before_sha=hashlib.sha256(before).hexdigest() if before is not None else None,
                     after_sha=hashlib.sha256(local).hexdigest()))
 print(('CHANGE ' if changed else 'KEEP   ')+path,flush=True)
database=client.read(remote_root+'/brandit_tickets_v1.db')
if database:
 db_path=folder/'before'/'brandit_tickets_v1.db'
 private_save(db_path,database)
 with sqlite3.connect('file:'+str(db_path)+'?mode=ro',uri=True) as conn:
  result=conn.execute('PRAGMA quick_check').fetchone()[0]
  if result!='ok': raise RuntimeError('Downloaded database did not pass validation')
  print('Hosted events:',conn.execute('SELECT COUNT(*) FROM Events').fetchone()[0],flush=True)
manifest=dict(remote_root=remote_root,domain=client.config['domain'],files=entries)
private_save(folder/'manifest.json',json.dumps(manifest,indent=2).encode())
private_save(ROOT/'.deploy'/'current-release.txt',str(folder).encode())
print('Prepared release:',folder,flush=True)
print('Changed files:',sum(e['changed'] for e in entries),flush=True)

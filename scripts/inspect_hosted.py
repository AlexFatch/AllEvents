import ast
import hashlib
import json
from pythonanywhere_client import PythonAnywhere, ROOT, private_save
client=PythonAnywhere()
folder=ROOT/'.deploy'/'inspection'
for remote, local in [('/home/anesu/flask_app.py','actual_flask_app.py'),('/home/anesu/config_local.py','config_local.py')]:
 data=client.read(remote)
 if data is None:
  print(remote, 'absent')
  continue
 private_save(folder/local,data)
 print(remote, 'downloaded privately; SHA256', hashlib.sha256(data).hexdigest())
 if local=='actual_flask_app.py':
  tree=ast.parse(data.decode())
  print('Lines:',len(data.splitlines()))
  print('Functions:',', '.join(n.name for n in tree.body if isinstance(n,ast.FunctionDef)))
  for n in tree.body:
   if isinstance(n,ast.Assign):
    target=', '.join(ast.unparse(x) for x in n.targets)
    if any(k in target for k in ['DB_NAME','BASE_DIR','DOMAIN_URL']):
     print('Path/config:',target,'=',ast.unparse(n.value))
    elif any(k in target.lower() for k in ['secret','password','paynow','smtp','key','token']):
     print('Config expression:',target, type(n.value).__name__)
result=client.request('GET','/webapps/'+client.config['domain']+'/static_files/')
result.raise_for_status()
private_save(folder/'static_mappings.json',json.dumps(result.json()).encode())
print('Static mappings:',json.dumps(result.json()))

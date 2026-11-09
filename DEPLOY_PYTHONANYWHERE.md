# Existing PythonAnywhere deployment

Target: `anesu.pythonanywhere.com` (account `anesu`).

The verified live app is `/home/anesu/flask_app.py`, using Python 3.10.
The Web tab's source directory `/home/anesu/mysite` contains an older app;
the WSGI file prepends `/home/anesu` and imports the app from there. Deploy
to the effective application directory rather than the older `mysite` folder.

## Configure local authentication

Open [Account → API token](https://www.pythonanywhere.com/account/#api_token),
then run this on your Mac:

```sh
cd /Users/mac/Downloads/eventsys
python3 scripts/configure_pythonanywhere.py
```

The prompt hides your input. Credentials are saved in `.pythonanywhere.json`
with owner-only permissions and are excluded from Git and release staging.
Do not paste the token into chat.

## Inspect before uploading

```sh
.venv/bin/python scripts/pythonanywhere_client.py
```

This makes read-only API requests to find the hosted source directory,
Python version, virtualenv and WSGI configuration. Copies of the hosted app
and WSGI file are saved privately under `.deploy/inspection/`. Review the
hosted app before replacing it: production may contain changes or configuration
that are absent from the local copy.

`scripts/inspect_hosted.py` additionally checks this account's effective app
and static mappings. Both inspection scripts keep sensitive source files
private and avoid printing credentials.

## Application update

`python3 scripts/prepare_release.py` stages an explicit set of application
files and their SHA256 hashes under `dist/pythonanywhere-update/`. It does not
upload anything. Deploy only reviewed changes after backing up the hosted
files. Keep the hosted database, `.secret_key`, `config_local.py`, WSGI file,
payment credentials, email settings and existing user uploads.

The three existing images used by the slideshow are included in staging, along
with the JavaScript, local scanner library, templates and event import files.
The event import is a separate action against the hosted database; copying
the JSON alone does not add events. Use its importer after backing up the
hosted database, rather than uploading your Mac's database over production.

PythonAnywhere serves Flask through its existing WSGI configuration. After
uploading, reload the existing web app, then check the homepage, a locally
ticketed event's buy page, scanner assets and admin users. Check the `/static/`
mapping points to the deployed static directory. Do not run `run_local.sh` on
the hosting account.

Official references:
[API](https://help.pythonanywhere.com/pages/API),
[Flask configuration](https://help.pythonanywhere.com/pages/Flask),
[static files](https://help.pythonanywhere.com/pages/StaticFiles/).

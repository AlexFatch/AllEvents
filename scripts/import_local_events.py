"""Import the reviewed event snapshot without creating local tickets or prices."""
import json
from pathlib import Path
import sqlite3
from datetime import datetime
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]


def import_events(database, records):
    conn = sqlite3.connect(database)
    try:
        columns = {r[1] for r in conn.execute('PRAGMA table_info(Events)')}
        if 'SourceURL' not in columns:
            conn.execute('ALTER TABLE Events ADD COLUMN SourceURL TEXT')
        inserted = 0
        with conn:
            # Serialise imports if several hosted workers start together.
            conn.execute('BEGIN IMMEDIATE')
            for event in records:
                source = event['source_url']
                if urlparse(source).scheme != 'https' or not urlparse(source).netloc:
                    raise ValueError('Expected an HTTPS source link')
                datetime.fromisoformat(event['date'])
                if conn.execute('SELECT 1 FROM Events WHERE Title=? AND EventDate=?',
                                (event['title'], event['date'])).fetchone():
                    continue
                conn.execute('''INSERT INTO Events
                    (Title, EventDate, Venue, Category, Description, ImageURL, IsActive, SourceURL)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?)''',
                    (event['title'], event['date'], event['venue'], event['category'],
                     event['description'], event['image_url'], source))
                inserted += 1
        return inserted
    finally:
        conn.close()


if __name__ == '__main__':
    database = ROOT / 'brandit_tickets_v1.db'
    backup_dir = ROOT / 'backups'
    backup_dir.mkdir(exist_ok=True)
    backup = backup_dir / ('before-event-import-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.db')
    with sqlite3.connect(database) as original, sqlite3.connect(backup) as snapshot:
        original.backup(snapshot)
    records = json.loads((ROOT / 'data/zimbabwe_events_2026.json').read_text())
    count = import_events(database, records)
    print(f'Added {count} external events. Backup: {backup}')

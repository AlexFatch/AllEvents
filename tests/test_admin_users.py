"""Run with: .venv/bin/python -m unittest discover -s tests"""
import importlib
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch


class AdminUsersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        connect = sqlite3.connect
        database = str(Path(cls.temp.name) / 'test.db')
        # Import runs migrations; redirect connections to a disposable database.
        with patch.dict(os.environ, LOCAL_DEV='1', ADMIN_PASSWORD='test-admin-password'), \
             patch('sqlite3.connect', side_effect=lambda *a, **kw: connect(database, **kw)):
            cls.module = importlib.import_module('flask_app')
        cls.module.DB_NAME = database
        cls.module.app.config.update(TESTING=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_user_lifecycle_and_permissions(self):
        app = self.module.app
        client = app.test_client()
        self.assertEqual(client.get('/admin/users').status_code, 302)
        with self.module.db() as conn:
            admin_id = conn.execute("SELECT UserID FROM Users WHERE Username='admin'").fetchone()[0]
        with client.session_transaction() as session:
            session.update(user_id=admin_id, role='Admin', assigned_event=None, last_active=time.time())
        self.assertEqual(client.get('/admin/users').status_code, 200)
        self.assertEqual(client.get('/admin/users/create').status_code, 200)
        data = dict(username="gate'1", password='initial-password', role='Bouncer', event_id='0')
        self.assertEqual(client.post('/admin/users/create', data=data, follow_redirects=True).status_code, 200)
        self.assertEqual(client.post('/admin/users/create', data=data).status_code, 409)
        with self.module.db() as conn:
            uid = conn.execute('SELECT UserID FROM Users WHERE Username=?', (data['username'],)).fetchone()[0]
            conn.execute('UPDATE Users SET IsLocked=1, FailedAttempts=5 WHERE UserID=?', (uid,))
        self.assertEqual(client.post(f'/admin/users/reset/{uid}', data={'password': 'short'}).status_code, 400)
        self.assertEqual(client.post(f'/admin/users/reset/{uid}', data={'password': 'replacement-password'}).status_code, 302)
        with self.module.db() as conn:
            user = conn.execute('SELECT * FROM Users WHERE UserID=?', (uid,)).fetchone()
            self.assertEqual(user['IsLocked'], 0)
            self.assertEqual(user['FailedAttempts'], 0)
            self.assertTrue(self.module.check_password_hash(user['PasswordHash'], 'replacement-password'))
        self.assertEqual(client.get(f'/admin/users/delete/{uid}').status_code, 405)
        self.assertEqual(client.post(f'/admin/users/delete/{admin_id}').status_code, 400)
        self.assertEqual(client.post(f'/admin/users/delete/{uid}', headers={'Origin': 'https://other.example'}).status_code, 403)
        self.assertEqual(client.post(f'/admin/users/delete/{uid}').status_code, 302)
        with self.module.db() as conn:
            self.assertIsNone(conn.execute('SELECT UserID FROM Users WHERE UserID=?', (uid,)).fetchone())
        with client.session_transaction() as session:
            session['assigned_event'] = 1
        self.assertEqual(client.get('/admin/users').status_code, 403)
        with client.session_transaction() as session:
            session.update(role='Cashier', assigned_event=None)
        self.assertEqual(client.get('/admin/users').status_code, 302)

    def test_scanner_lookup_pin_and_duplicate_checkin(self):
        m = self.module
        client = m.app.test_client()
        self.assertEqual(client.post('/api/gate/lookup', json={'code': 'GATE-TEST'}).status_code, 401)
        with m.db() as conn:
            event_id = conn.execute("INSERT INTO Events (Title, EventDate) VALUES ('Gate test', '2099-12-01')").lastrowid
            conn.execute('''INSERT INTO EventTickets
                (EventID, TicketCode, GuestName, TicketType, AccessPin, PaymentStatus)
                VALUES (?, 'GATE-TEST', 'Test guest', 'Standard', '1234', 'Paid')''', (event_id,))
        with client.session_transaction() as session:
            session.update(user_id=1, role='Bouncer', assigned_event=event_id, last_active=time.time())
        for path in ['/scanner', '/scan_home']:
            page = client.get(path)
            self.assertEqual(page.status_code, 200)
            self.assertIn('/static/html5-qrcode.min.js', page.get_data(as_text=True))
            self.assertNotIn('cdnjs.cloudflare.com', page.get_data(as_text=True))
        with client.get('/static/html5-qrcode.min.js') as asset:
            self.assertEqual(asset.status_code, 200)
        self.assertEqual(client.post('/api/gate/lookup', json={'code': 'https://example.com/gate/GATE-TEST'}).json['status'], 'ok')
        self.assertEqual(client.post('/api/gate/verify', json={'code': 'GATE-TEST', 'pin': '9999'}).json['status'], 'wrong_pin')
        with client.session_transaction() as session:
            session['assigned_event'] = event_id + 100
        self.assertEqual(client.post('/api/gate/verify', json={'code': 'GATE-TEST', 'pin': '1234'}).json['status'], 'wrong_event')
        with client.session_transaction() as session:
            session['assigned_event'] = event_id
        result = client.post('/api/gate/verify', json={'code': 'GATE-TEST', 'pin': '1234'}).json
        self.assertEqual(result['status'], 'granted')
        self.assertEqual(result['inside'], 1)
        self.assertEqual(client.post('/api/gate/verify', json={'code': 'GATE-TEST', 'pin': '1234'}).json['status'], 'used')

    def test_external_event_import_and_booking(self):
        from scripts.import_local_events import import_events
        import json
        m = self.module
        records = json.loads(Path('data/zimbabwe_events_2026.json').read_text())
        self.assertEqual(import_events(m.DB_NAME, records), len(records))
        self.assertEqual(import_events(m.DB_NAME, records), 0)
        client = m.app.test_client()
        page = client.get('/')
        self.assertEqual(page.status_code, 200)
        self.assertIn('External listing', page.get_data(as_text=True))
        with m.db() as conn:
            events = conn.execute('SELECT * FROM Events WHERE SourceURL IS NOT NULL').fetchall()
            for event in events:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM EventTickets WHERE EventID=?', (event['EventID'],)).fetchone()[0], 0)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM EventPrices WHERE EventID=?', (event['EventID'],)).fetchone()[0], 0)
                buy_url = '/buy' if event['EventID'] == 1 else '/buy/' + str(event['EventID'])
                response = client.get(buy_url)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.location, event['SourceURL'])
                self.assertEqual(client.post(buy_url).status_code, 400)
        with client.session_transaction() as session:
            session.update(user_id=1, role='Admin', assigned_event=None, last_active=time.time())
        self.assertEqual(client.get('/admin/events').status_code, 200)
        for event in events:
            self.assertEqual(client.get('/admin/event/edit/' + str(event['EventID'])).status_code, 200)

    def test_landing_slideshow_and_checkout_styling(self):
        from unittest.mock import patch
        from html.parser import HTMLParser
        class Elements(HTMLParser):
            def __init__(self, text):
                super().__init__()
                self.items = []
                self.feed(text)
            def handle_starttag(self, tag, attrs):
                self.items.append((tag, dict(attrs)))
        m = self.module
        client = m.app.test_client()
        home = client.get('/').get_data(as_text=True)
        self.assertIn('id="heroSlideshow"', home)
        self.assertEqual(home.count('class="hero-slide'), 4)  # three photos plus their wrapper
        for path in ['js/hero-slideshow.js', 'js/checkout.js', 'uploads/audience-1853662_1280.jpg',
                     'uploads/7907ba4ae1584aac9801bc616493ac89.jpg', 'uploads/0a1afd8f67434fd4b5cd06d68c371208.jpeg']:
            with client.get('/static/' + path) as response:
                self.assertEqual(response.status_code, 200)
        with m.db() as conn:
            eid = conn.execute("INSERT INTO Events (Title, EventDate, Venue, Category) VALUES ('Checkout test', '2099-12-01', 'Harare', 'Concert')").lastrowid
            conn.execute("INSERT INTO EventPrices (EventID, TicketType, Price) VALUES (?, 'Standard', 12.5)", (eid,))
            for n in range(3):
                conn.execute("INSERT INTO EventTickets (EventID, TicketCode, TicketType, PaymentStatus) VALUES (?, ?, 'Standard', 'Available')", (eid, 'CHECKOUT-' + str(n)))
        with patch.object(m, 'paynow', None):
            text = client.get('/buy/' + str(eid)).get_data(as_text=True)
            self.assertIn('Online payments are temporarily unavailable', text)
            self.assertIn('ALL<span>Events</span>', text)
            self.assertNotIn('SDA Events', text)
            fields = Elements(text).items
            quantity = next(a for tag, a in fields if tag == 'input' and a.get('name') == 'quantity')
            self.assertEqual(quantity['max'], str(m.MAX_TICKETS_PER_ORDER))
            option = next(a for tag, a in fields if tag == 'option' and a.get('value') == 'Standard')
            self.assertEqual(option['data-stock'], '3')
            self.assertEqual(option['data-price'], '12.5')
            button = next(a for tag, a in fields if tag == 'button' and a.get('type') == 'submit')
            self.assertIn('disabled', button)
        with patch.object(m, 'paynow', object()):
            fields = Elements(client.get('/buy/' + str(eid)).get_data(as_text=True)).items
            button = next(a for tag, a in fields if tag == 'button' and a.get('type') == 'submit')
            self.assertNotIn('disabled', button)

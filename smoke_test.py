"""Small local smoke test for EasyKada routes and shop-user authentication."""
import os
import tempfile

# Use an isolated database for the smoke test.
_fd, _db_path = tempfile.mkstemp(prefix="easykada-test-", suffix=".sqlite3")
os.close(_fd)
os.environ["EASYKADA_DATABASE_PATH"] = _db_path
os.environ["EASYKADA_SECRET_KEY"] = "test-only-secret-key-change-me"

import app as easykada

client = easykada.app.test_client()

# Public health check and login page.
r = client.get('/healthz')
assert r.status_code == 200, (r.status_code, r.data[:500])
assert client.get('/login').status_code == 200

# Admin login.
r = client.post('/login', data={'username': 'admin', 'password': 'admin123'}, follow_redirects=True)
assert r.status_code == 200, (r.status_code, r.data[:1000])
assert b'Admin Console' in r.data, r.data[:1000]

# Create a shop account, then log in using that new account.
r = client.post('/admin/create', data={
    'name': 'Smoke Test Shop',
    'username': 'smokeshop',
    'password': 'ShopPass123!',
    'phone': '',
    'address': '',
}, follow_redirects=True)
assert r.status_code == 200, (r.status_code, r.data[:1000])

client.get('/logout')
r = client.post('/login', data={'username': 'smokeshop', 'password': 'ShopPass123!'}, follow_redirects=True)
assert r.status_code == 200, (r.status_code, r.data[:1500])
assert b'Shop Dashboard' in r.data, r.data[:1500]

# Ensure the route that previously failed responds successfully after shop login.
r = client.get('/shop')
assert r.status_code == 200, (r.status_code, r.data[:1500])

print('EasyKada smoke test passed: health, admin login, create shop, shop login, dashboard.')
try:
    os.unlink(_db_path)
except OSError:
    pass

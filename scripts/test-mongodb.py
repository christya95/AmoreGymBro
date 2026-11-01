"""Run storage integration tests using injected local test-database credentials."""
import os
import subprocess
from dotenv import dotenv_values

values = dotenv_values('.env')
password = os.environ.get('MONGO_INITDB_ROOT_PASSWORD')
if not password:
    raise SystemExit('Unlock 1Password and run ./scripts/test-mongodb.sh.')
env = dict(os.environ, RUN_MONGODB_TESTS='1',
    TEST_MONGODB_URI='mongodb://127.0.0.1:27018',
    TEST_MONGODB_USERNAME=values.get('MONGO_INITDB_ROOT_USERNAME', 'amoregymbro-admin'),
    TEST_MONGODB_PASSWORD=password)
subprocess.run(['docker','compose','-p','amoregymbro','-f','docker-compose.yml','-f','docker-compose.local.yml','up','-d','--wait','mongodb'],check=True)
raise SystemExit(subprocess.run(['uv', 'run', 'pytest', '-q'], env=env).returncode)

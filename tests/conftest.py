import os
import tempfile

# Test-Umgebung, bevor app.config importiert wird
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/test.db"
os.environ.setdefault("HA_URL", "http://ha.test")
os.environ.setdefault("HA_TOKEN", "dummy")
os.environ.setdefault("SMTP_HOST", "smtp.test")
os.environ.setdefault("SMTP_FROM", "abrechnung@test.de")
os.environ["VICTRON_LOGGER"] = "0"  # Hintergrund-Logger in Tests nicht starten
os.environ["APP_ENCRYPTION_KEY"] = "test-key"

import pytest  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line("markers", "totp: Test mit verpflichtender Zwei-Faktor-Anmeldung")


@pytest.fixture(autouse=True)
def fresh_db(request):
    """Jeder Test startet mit leerer Datenbank."""
    from app import db

    db.Base.metadata.drop_all(db.engine)
    db.init_db()
    if not request.node.get_closest_marker("totp"):  # 2FA-Pflicht nur in den 2FA-Tests
        with db.SessionLocal() as s:
            db.save_settings(s, {"totp_admins": "0"})
    yield

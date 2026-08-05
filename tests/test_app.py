from fastapi.testclient import TestClient

from kanakko import __version__
from kanakko.app import app


def test_healthz_reports_ok_and_version():
    response = TestClient(app).get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}

"""Les suites sans historique ne touchent jamais à la base réelle du Mac."""

import pytest


@pytest.fixture(autouse=True)
def isolate_history(monkeypatch):
    # Les tests de stockage activent explicitement le port avec un tmp_path.
    monkeypatch.setenv("HISTORY_ENABLED", "false")
    monkeypatch.setenv("VOICEMAIL_ENABLED", "false")
    # Les webhooks synthétiques ne viennent pas des serveurs Twilio.
    # Les tests de sécurité activent explicitement leur validation.
    monkeypatch.setenv("TWILIO_VALIDATE_SIGNATURE", "false")

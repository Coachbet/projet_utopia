"""Proxy audio : auth serveur, destination fixe et taille bornée."""

import base64

import httpx
import pytest

from vocalguard.config import Settings
from vocalguard.infrastructure.telephony.recordings import RecordingUnavailable, TwilioRecordings

SID = "RE" + "a" * 32


def adapter(handler):
    return TwilioRecordings(
        Settings(
            _env_file=None,
            twilio_account_sid="AC_test",
            twilio_api_key_sid="SK_test",
            twilio_api_key_secret="fake-key",
        ),
        transport=httpx.MockTransport(handler),
    )


async def test_media_retrieved_from_account_api_with_api_key_only():
    def handle(request):
        assert (
            str(request.url)
            == f"https://api.twilio.com/2010-04-01/Accounts/AC_test/Recordings/{SID}.mp3"
        )
        assert (
            request.headers["authorization"]
            == "Basic " + base64.b64encode(b"SK_test:fake-key").decode()
        )
        return httpx.Response(200, content=b"fake-mp3")

    assert await adapter(handle).audio(SID) == b"fake-mp3"


async def test_untrusted_recording_url_never_creates_http_request():
    def handle(request):
        pytest.fail("unexpected HTTP request")

    with pytest.raises(RecordingUnavailable):
        await adapter(handle).audio("https://evil.invalid/secret")


@pytest.mark.parametrize("status", [301, 401, 404, 500])
async def test_upstream_errors_do_not_expose_url_or_credentials(status):
    with pytest.raises(RecordingUnavailable) as failure:
        await adapter(lambda request: httpx.Response(status)).audio(SID)
    assert "fake-key" not in str(failure.value)
    assert SID not in str(failure.value)


async def test_provider_audio_size_is_bounded():
    with pytest.raises(RecordingUnavailable):
        await adapter(lambda request: httpx.Response(200, content=b"x" * 2_000_001)).audio(SID)


async def test_delete_is_idempotent_for_already_removed_recording():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(404)

    await adapter(handle).delete(SID)
    assert requests[0].method == "DELETE"
    assert requests[0].url.path.endswith(SID + ".json")

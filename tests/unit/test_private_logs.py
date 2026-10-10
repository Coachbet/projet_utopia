from vocalguard.domain.events import Event, EventType
from vocalguard.infrastructure.fakes.call_controller import LoggingCallController
from vocalguard.infrastructure.fakes.event_sink import ConsoleEventSink


async def test_console_never_prints_conversation_or_verification_content(capsys):
    sink = ConsoleEventSink()
    private = "Le code SMS personnel est 9821"
    for kind, data in (
        (EventType.TRANSCRIPT, {"text": private, "is_final": True}),
        (EventType.LLM, {"explanation": private, "risk": 0.8}),
        (EventType.ACTION, {"action": "announce_victim", "message": private, "ok": True}),
    ):
        await sink.emit(Event(call_id="CA_logs", t=1, type=kind, data=data))
    output = capsys.readouterr().out
    assert private not in output
    assert "9821" not in output
    assert "transcript" in output
    assert '"risk": 0.8' in output


async def test_fake_controller_does_not_log_sms_or_spoken_code(caplog):
    controller = LoggingCallController()
    with caplog.at_level("INFO"):
        await controller.send_sms("+33600000000", "Code secret : 9821")
        await controller.announce_to_victim("CA_logs", "Code secret : 9821")
    assert "9821" not in caplog.text
    assert "send_sms" in caplog.text

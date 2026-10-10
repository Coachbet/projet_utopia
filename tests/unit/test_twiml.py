"""Le TwiML est du contrat avec Twilio : on asserte sur la structure XML, pas sur
des chaînes, pour que le test survive à un changement d'ordre des attributs."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from vocalguard.infrastructure.telephony.twiml import (
    DEFAULT_HOLD_MUSIC_URL,
    announce_twiml,
    empty_twiml,
    hold_music_twiml,
    inbound_call_twiml,
    join_conference_twiml,
)

STREAM_URL = "wss://a1b2.ngrok-free.app/ws/media"
CONFERENCE = "utopia-guard-CA123"
CALLBACK = "https://a1b2.ngrok-free.app/twilio/conference/status"


def root(xml: str) -> ET.Element:
    element = ET.fromstring(xml)
    assert element.tag == "Response"
    return element


class TestInboundCall:
    def test_stream_captures_both_tracks(self) -> None:
        """`both_tracks` est ce qui sépare appelant et victime : sans ça, on ne sait
        plus qui parle et le scoring n'a plus de sens."""
        stream = root(inbound_call_twiml(stream_url=STREAM_URL, conference_name=CONFERENCE)).find(
            "Start/Stream"
        )
        assert stream is not None
        assert stream.get("track") == "both_tracks"
        assert stream.get("url") == STREAM_URL

    def test_le_numero_appelant_voyage_dans_le_stream(self) -> None:
        """Twilio ne met pas le `From` dans la trame `start` : ce `<Parameter>` est
        la seule source du numéro appelant pour une session ouverte depuis
        `/ws/media`. Sans lui, le signal de réputation reste inerte et le
        coupe-circuit « paiement + identité » ne peut jamais s'armer."""
        stream = root(
            inbound_call_twiml(
                stream_url=STREAM_URL,
                conference_name=CONFERENCE,
                caller_number="+33755555555",
            )
        ).find("Start/Stream")
        assert stream is not None
        parameters = {p.get("name"): p.get("value") for p in stream.findall("Parameter")}
        assert parameters == {"caller_number": "+33755555555"}

    def test_pas_de_parametre_vide_quand_le_numero_manque(self) -> None:
        """Un appelant masqué ne doit pas produire `<Parameter value="">` : côté
        lecture ça donnerait un numéro vide indistinguable d'un paramètre absent,
        mais en masquant la cause réelle."""
        stream = root(inbound_call_twiml(stream_url=STREAM_URL, conference_name=CONFERENCE)).find(
            "Start/Stream"
        )
        assert stream is not None
        assert stream.findall("Parameter") == []

    def test_stream_comes_before_dial(self) -> None:
        """Ordre imposé : brancher le flux après le `<Dial>` perdrait les premières
        secondes, celles où l'appelant annonce son identité."""
        children = [
            child.tag
            for child in root(inbound_call_twiml(stream_url=STREAM_URL, conference_name=CONFERENCE))
        ]
        assert children.index("Start") < children.index("Dial")

    def test_conference_settings(self) -> None:
        conference = root(
            inbound_call_twiml(stream_url=STREAM_URL, conference_name=CONFERENCE)
        ).find("Dial/Conference")
        assert conference is not None
        assert conference.text == CONFERENCE
        assert conference.get("startConferenceOnEnter") == "true"
        assert conference.get("endConferenceOnExit") == "true"
        assert conference.get("beep") == "false"
        # waitUrl vide = pas de musique d'attente dans la piste outbound.
        assert conference.get("waitUrl") == ""

    def test_status_callback_optional(self) -> None:
        without = root(inbound_call_twiml(stream_url=STREAM_URL, conference_name=CONFERENCE)).find(
            "Dial/Conference"
        )
        assert without is not None
        assert without.get("statusCallback") is None

        with_callback = root(
            inbound_call_twiml(
                stream_url=STREAM_URL,
                conference_name=CONFERENCE,
                conference_status_callback=CALLBACK,
            )
        ).find("Dial/Conference")
        assert with_callback is not None
        assert with_callback.get("statusCallback") == CALLBACK
        # `join` est l'événement qui nous dit que la victime est entrée.
        assert "join" in (with_callback.get("statusCallbackEvent") or "")


class TestJoinConference:
    def test_no_second_stream(self) -> None:
        """Un `<Stream>` sur la jambe sortante doublerait l'audio envoyé à Transcribe."""
        element = root(join_conference_twiml(conference_name=CONFERENCE))
        assert element.find("Start") is None
        assert element.find(".//Stream") is None

    def test_joins_same_conference(self) -> None:
        conference = root(join_conference_twiml(conference_name=CONFERENCE)).find("Dial/Conference")
        assert conference is not None
        assert conference.text == CONFERENCE
        assert conference.get("startConferenceOnEnter") == "true"
        assert conference.get("endConferenceOnExit") == "true"
        assert conference.get("beep") == "false"


class TestAnnounce:
    def test_speaks_french_by_default(self) -> None:
        say = root(announce_twiml("Attention, cet appel présente des signes d'arnaque.")).find(
            "Say"
        )
        assert say is not None
        assert say.get("language") == "fr-FR"
        assert say.text == "Attention, cet appel présente des signes d'arnaque."

    def test_language_override(self) -> None:
        say = root(announce_twiml("Careful", language="en-US")).find("Say")
        assert say is not None
        assert say.get("language") == "en-US"

    def test_escapes_markup(self) -> None:
        """L'explication vient de Claude, donc du texte libre : un `&` non échappé
        produirait un XML invalide et Twilio ne dirait rien du tout."""
        xml = announce_twiml("Virement & <urgent> réclamé")
        assert "&amp;" in xml
        assert "<urgent>" not in xml
        say = root(xml).find("Say")
        assert say is not None
        assert say.text == "Virement & <urgent> réclamé"


class TestHoldAndEmpty:
    def test_loops_forever(self) -> None:
        """Si la lecture s'arrêtait, Twilio sortirait le scammeur de l'attente."""
        play = root(hold_music_twiml()).find("Play")
        assert play is not None
        assert play.get("loop") == "0"
        assert play.text == DEFAULT_HOLD_MUSIC_URL

    def test_custom_music(self) -> None:
        play = root(hold_music_twiml(music_url="https://example.test/m.wav")).find("Play")
        assert play is not None
        assert play.text == "https://example.test/m.wav"

    def test_empty_response_is_valid_xml(self) -> None:
        assert len(root(empty_twiml())) == 0

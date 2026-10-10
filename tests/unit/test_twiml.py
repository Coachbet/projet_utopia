"""Le TwiML est du contrat avec Twilio : on asserte sur la structure XML, pas sur
des chaînes, pour que le test survive à un changement d'ordre des attributs."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from vocalguard.infrastructure.telephony.twiml import (
    DEFAULT_HOLD_MUSIC_URL,
    STREAM_TRACK_BOTH,
    STREAM_TRACK_VICTIM_ONLY,
    announce_twiml,
    empty_twiml,
    hold_music_twiml,
    inbound_call_twiml,
    join_conference_twiml,
    refusal_twiml,
    ringback_twiml,
    unavailable_twiml,
)

STREAM_URL = "wss://a1b2.ngrok-free.app/ws/media"
CONFERENCE = "utopia-guard-CA123"
CALLBACK = "https://a1b2.ngrok-free.app/twilio/conference/status"
NOTICE = "This call is being analyzed by a fraud protection service."


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
        assert conference.get("startConferenceOnEnter") == "false"
        assert conference.get("endConferenceOnExit") == "true"
        assert conference.get("beep") == "false"
        # La route fournit une URL publique de retour d'appel.
        assert conference.get("waitUrl") is None

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


class TestAnnonceAlAppelant:
    """L'ordre `Say` → `Start/Stream` → `Dial` est le montage juridique du produit.

    Ce n'est pas une préférence de mise en page : tout ce que Twilio exécute après
    l'annonce est postérieur à l'information de l'appelant. Un `<Stream>` placé
    avant capterait de l'audio qu'on n'a pas le droit d'analyser, et rien dans le
    reste du système ne pourrait le rattraper après coup.
    """

    def test_l_annonce_precede_le_stream_et_la_conference(self) -> None:
        children = [
            child.tag
            for child in root(
                inbound_call_twiml(
                    stream_url=STREAM_URL,
                    conference_name=CONFERENCE,
                    caller_notice=NOTICE,
                )
            )
        ]
        assert children.index("Say") < children.index("Start") < children.index("Dial")

    def test_l_annonce_est_dite_en_anglais(self) -> None:
        say = root(
            inbound_call_twiml(
                stream_url=STREAM_URL, conference_name=CONFERENCE, caller_notice=NOTICE
            )
        ).find("Say")
        assert say is not None
        assert say.get("language") == "en-US"
        assert say.text == NOTICE

    def test_aucune_annonce_sans_texte(self) -> None:
        """Le défaut reste muet : l'appel de confiance passe par la même fonction,
        et une annonce par défaut informerait la famille d'une analyse qui n'a
        pas lieu."""
        element = root(inbound_call_twiml(stream_url=STREAM_URL, conference_name=CONFERENCE))
        assert element.find("Say") is None

    def test_annonce_bilingue_avant_toute_capture(self) -> None:
        element = root(
            inbound_call_twiml(
                stream_url=STREAM_URL,
                conference_name=CONFERENCE,
                caller_notice=NOTICE,
                caller_notice_bilingual=True,
            )
        )
        assert [child.tag for child in element] == ["Say", "Say", "Start", "Dial"]
        assert [say.get("language") for say in element.findall("Say")] == ["en-US", "fr-FR"]
        assert element.findall("Say")[1].text.startswith("Cet appel est analysé")

    def test_langue_configurable_pour_l_appelant_seul(self) -> None:
        element = root(
            inbound_call_twiml(
                stream_url=STREAM_URL,
                conference_name=CONFERENCE,
                caller_notice="Cet appel est analysé.",
                caller_notice_language="fr-FR",
            )
        )
        assert element.find("Say").get("language") == "fr-FR"


def test_la_tonalite_attend_la_victime_et_se_repete() -> None:
    wait_url = "https://demo.example/twilio/voice/ringback"
    conference = root(
        inbound_call_twiml(
            stream_url=STREAM_URL,
            conference_name=CONFERENCE,
            caller_wait_url=wait_url,
        )
    ).find("Dial/Conference")
    assert conference.get("startConferenceOnEnter") == "false"
    assert conference.get("waitUrl") == wait_url
    assert conference.get("waitMethod") == "GET"
    waiting = root(ringback_twiml("https://demo.example/ring.wav"))
    assert [child.tag for child in waiting] == ["Play", "Redirect"]
    assert waiting.find("Redirect").text is None


def test_indisponible_annonce_en_anglais_avant_de_finir_la_jambe() -> None:
    response = root(unavailable_twiml())
    assert [child.tag for child in response] == ["Say", "Hangup"]
    assert response.find("Say").get("language") == "en-US"
    assert response.find("Say").text == "The person is not available."


class TestAppelDeConfiance:
    """`track=None` : la forme servie à un contact de la liste blanche."""

    def test_ni_stream_ni_annonce(self) -> None:
        """Le cœur de la promesse produit : la famille n'est pas écoutée, et
        l'appelant n'entend rien qui lui apprenne qu'un service existe — donc
        rien qui lui dise qu'il a été reconnu."""
        element = root(
            inbound_call_twiml(
                stream_url=STREAM_URL,
                conference_name=CONFERENCE,
                caller_number="+33672019048",
                track=None,
            )
        )
        assert element.find("Start") is None
        assert element.find(".//Stream") is None
        assert element.find("Say") is None

    def test_la_conference_reste_identique(self) -> None:
        """Mêmes réglages de conférence qu'un appel analysé, `waitUrl` compris :
        une musique d'attente qui ne se déclencherait que pour les appelants
        reconnus apprendrait à qui rappelle deux fois s'il est au carnet."""
        conference = root(
            inbound_call_twiml(stream_url=STREAM_URL, conference_name=CONFERENCE, track=None)
        ).find("Dial/Conference")
        assert conference is not None
        assert conference.text == CONFERENCE
        assert conference.get("startConferenceOnEnter") == "false"
        assert conference.get("endConferenceOnExit") == "true"
        assert conference.get("beep") == "false"
        assert conference.get("waitUrl") is None

    def test_piste_de_la_personne_protegee_seule(self) -> None:
        """Lot 3 : on écoute l'abonnée qui a consenti, jamais son interlocuteur.
        Si cette piste devenait `both_tracks`, la promesse serait rompue sans que
        rien d'autre ne change dans le système."""
        element = root(
            inbound_call_twiml(
                stream_url=STREAM_URL,
                conference_name=CONFERENCE,
                track=STREAM_TRACK_VICTIM_ONLY,
            )
        )
        stream = element.find("Start/Stream")
        assert stream is not None
        assert stream.get("track") == "outbound_track"
        # Toujours aucune annonce : l'appelant de confiance n'est pas analysé,
        # c'est la voix de la personne protégée qui l'est.
        assert element.find("Say") is None

    def test_les_deux_pistes_restent_le_defaut(self) -> None:
        """Le défaut écoute, le refus se demande : une absence d'écoute ne doit
        jamais pouvoir découler d'un paramètre oublié."""
        stream = root(inbound_call_twiml(stream_url=STREAM_URL, conference_name=CONFERENCE)).find(
            "Start/Stream"
        )
        assert stream is not None
        assert stream.get("track") == STREAM_TRACK_BOTH


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

    def test_l_annonce_a_la_victime_precede_la_conference(self) -> None:
        """Avant la conférence = entendu par la personne protégée SEULE : l'appelant
        n'est pas encore sur la même ligne qu'elle. C'est ce qui permet de lui dire
        qui appelle, alors que son écran n'affiche que notre numéro Twilio."""
        element = root(
            join_conference_twiml(
                conference_name=CONFERENCE, victim_notice="Appel de Lucas Martin."
            )
        )
        children = [child.tag for child in element]
        assert children.index("Say") < children.index("Dial")
        say = element.find("Say")
        assert say is not None
        assert say.get("language") == "fr-FR"
        assert say.text == "Appel de Lucas Martin."

    def test_aucune_annonce_sans_texte(self) -> None:
        """Session introuvable côté route : pas d'annonce plutôt qu'une fausse."""
        assert root(join_conference_twiml(conference_name=CONFERENCE)).find("Say") is None


class TestRefus:
    def test_une_phrase_et_aucune_conference(self) -> None:
        """La garde anti-boucle refuse d'acheminer : surtout pas de `<Dial>`, qui
        rappellerait la victime et bouclerait à deux jambes facturées par tour."""
        element = root(refusal_twiml("Cet appel ne peut pas aboutir."))
        say = element.find("Say")
        assert say is not None
        assert say.get("language") == "fr-FR"
        assert say.text == "Cet appel ne peut pas aboutir."
        assert element.find("Dial") is None
        assert element.find(".//Stream") is None


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

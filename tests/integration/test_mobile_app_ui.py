"""Contrat de la page mobile servie, sans SDK ni réseau tiers.

Les captures et les comportements asynchrones sont vérifiés séparément dans
scripts/check_mobile_ui.cjs. Ici, le serveur doit fournir une page complète :
aucun contrôle sans cible DOM, aucun doublon d'identifiant, SDK épinglé et
feuilles accessibles avec une vraie consigne de sécurité.
"""

from html.parser import HTMLParser

from fastapi import FastAPI
from fastapi.testclient import TestClient

from vocalguard.interface.http.phone_app import router


class Markup(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.dialogs: list[dict[str, str | None]] = []
        self.external_scripts: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if "id" in attributes:
            self.ids.append(str(attributes["id"]))
        if attributes.get("role") == "dialog":
            self.dialogs.append(attributes)
        if tag == "script" and "src" in attributes:
            self.external_scripts.append(attributes)


def served_markup() -> tuple[str, Markup]:
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get("/app")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    markup = Markup()
    markup.feed(response.text)
    return response.text, markup


def test_complete_mobile_screen_contract_and_unique_targets() -> None:
    _, markup = served_markup()
    assert len(markup.ids) == len(set(markup.ids)), "Un identifiant doit viser un seul élément"
    required = {
        "ecran-pin",
        "ecran-attente",
        "ecran-appel",
        "ecran-fin",
        "ecran-historique",
        "ecran-detail",
        "ecran-contacts",
        "ecran-numerotation",
        "ecran-messagerie",
        "telephone-onglets",
        "numerotation-numero",
        "btn-appeler",
        "contacts-recherche",
        "messagerie-confirmation",
        "appel-avatar",
        "appel-provenance",
        "appel-statut",
        "btn-decrocher",
        "btn-refuser",
        "btn-muet",
        "btn-clavier",
        "btn-haut-parleur",
        "btn-verifier",
        "btn-attente",
        "btn-raccrocher",
        "historique-confirmation",
    }
    assert required <= set(markup.ids)


def test_security_notice_accessible_dialogs_and_pinned_sdk() -> None:
    html, markup = served_markup()
    assert "Never say this code." in html
    assert "They must read it to you." in html
    assert "Keep this page open" in html
    assert len(markup.external_scripts) == 1
    sdk = markup.external_scripts[0]
    assert "@twilio/voice-sdk@2.18.5/" in str(sdk["src"])
    assert str(sdk["integrity"]).startswith("sha384-")
    assert sdk["crossorigin"] == "anonymous"
    assert len(markup.dialogs) == 6
    for dialog in markup.dialogs:
        assert dialog["aria-modal"] == "true"
        assert dialog["aria-labelledby"] in markup.ids

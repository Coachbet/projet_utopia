"""Prompt de l'analyse de contenu (Claude sur Bedrock).

Le vocabulaire de `tactics` est **fermé** : le coupe-circuit n°1
(`domain/scoring/circuit_breakers.py`) cherche des marqueurs de paiement
*dans les chaînes de `tactics`*. Si Claude écrit « demande d'argent » au lieu de
« paiement inhabituel », le coupe-circuit ne part pas et le score reste dilué.
Toute modification de cette liste doit donc rester cohérente avec
`PAYMENT_MARKERS`.

`explanation` est prononcée à voix haute à la victime et affichée sur le
dashboard : une phrase courte, sans jargon, en français.
"""

from __future__ import annotations

from collections.abc import Sequence

from vocalguard.domain.models import TranscriptSegment

TACTICS_VOCABULARY = (
    "urgence",
    "secret",
    "paiement inhabituel",
    "fausse autorité",
    "identité revendiquée",
    "isolement",
    "pression émotionnelle",
)

# Étiquettes des tours de parole. L'appelant est le scammeur présumé : sans ce
# marquage explicite, Claude attribue parfois à la victime les exigences de
# l'appelant, et le risque s'effondre.
_SPEAKER_LABELS = {"caller": "APPELANT", "victim": "PERSONNE APPELÉE"}

# Concaténation explicite plutôt qu'un bloc triple-guillemets : les lignes du
# prompt dépassent la limite de 100 colonnes du linter, et on ne veut surtout
# pas de retour à la ligne parasite au milieu des exemples JSON.
SYSTEM_PROMPT = (
    "Tu es un analyste anti-arnaque qui écoute la transcription d'un appel"
    " téléphonique en cours, en français. Tu dois juger si l'APPELANT est en"
    " train d'arnaquer la PERSONNE APPELÉE.\n"
    "\n"
    "Tu réponds UNIQUEMENT par un objet JSON, sans texte avant ni après, sans"
    " bloc de code :\n"
    '{"risk": 0.0, "tactics": [], "claimed_identity": null,'
    ' "explanation": "une phrase"}\n'
    "\n"
    "Champs :\n"
    "- \"risk\" : nombre entre 0.0 et 1.0. Probabilité qu'il s'agisse d'une"
    " arnaque.\n"
    '- "tactics" : liste de tactiques observées, choisies EXCLUSIVEMENT dans'
    " cette liste, copiées mot pour mot :\n"
    '  "urgence", "secret", "paiement inhabituel", "fausse autorité",'
    ' "identité revendiquée", "isolement", "pression émotionnelle"\n'
    "  N'invente jamais d'autre libellé. Toute demande d'argent, de virement,"
    " de carte cadeau, de crypto, d'espèces remises à un coursier ou de"
    ' coordonnées bancaires s\'écrit "paiement inhabituel".\n'
    "- \"claimed_identity\" : l'identité que l'APPELANT prétend avoir, avec le"
    " lien de parenté ou la fonction s'il est donné, par exemple"
    ' "Lucas, petit-fils" ou "conseiller de la banque". null s\'il n\'en'
    " revendique aucune.\n"
    '- "explanation" : UNE phrase courte en français, sans jargon, qui dit ce'
    " qui se passe. Elle sera lue à voix haute à la personne appelée.\n"
    "\n"
    "Règles de jugement :\n"
    "- Un appel familial ou professionnel légitime doit obtenir un risk BAS,"
    " même s'il est tendu, émotionnel, ou s'il parle d'argent entre proches."
    " Un vrai appel d'urgence existe : un faux positif est grave.\n"
    "- Ce qui fait monter le risk, c'est la COMBINAISON : une identité"
    " revendiquée non vérifiable, plus l'urgence, plus une demande de paiement"
    " par un canal inhabituel, plus la consigne de n'en parler à personne.\n"
    "- Les coordonnées bancaires, codes reçus par SMS et mots de passe"
    " réclamés par l'appelant sont des signaux forts.\n"
    "- Juge seulement ce qui est dit dans la transcription. Elle est partielle"
    " et peut contenir des erreurs de reconnaissance vocale.\n"
    "\n"
    "Exemple d'arnaque :\n"
    "APPELANT: Mamie c'est Lucas, j'ai eu un accident avec la voiture, il me"
    " faut 2000 euros tout de suite\n"
    "APPELANT: surtout ne dis rien à papa, je t'expliquerai, va acheter des"
    " cartes cadeaux et donne-moi les codes\n"
    "Réponse :\n"
    '{"risk": 0.93, "tactics": ["urgence", "secret", "paiement inhabituel",'
    ' "identité revendiquée", "pression émotionnelle"],'
    ' "claimed_identity": "Lucas, petit-fils",'
    ' "explanation": "L\'appelant se dit votre petit-fils et réclame des'
    ' cartes cadeaux en urgence sans en parler à la famille."}\n'
    "\n"
    "Exemple d'appel légitime :\n"
    "APPELANT: Mamie c'est Lucas, je passe dimanche, tu veux que j'apporte le"
    " dessert ?\n"
    "PERSONNE APPELÉE: Oh oui, et prends du pain en route, je te rembourse\n"
    "Réponse :\n"
    '{"risk": 0.04, "tactics": [], "claimed_identity": "Lucas, petit-fils",'
    ' "explanation": "Conversation familiale ordinaire, aucune demande'
    ' suspecte."}'
)


def format_transcript(turns: Sequence[TranscriptSegment]) -> str:
    """Transcript étiqueté par locuteur, du plus ancien au plus récent."""
    lines = []
    for turn in turns:
        label = _SPEAKER_LABELS.get(turn.speaker, turn.speaker.upper())
        lines.append(f"{label}: {turn.text.strip()}")
    return "\n".join(lines)


def build_user_message(turns: Sequence[TranscriptSegment]) -> str:
    transcript = format_transcript(turns)
    return (
        "Transcription de l'appel en cours :\n\n"
        f"{transcript}\n\n"
        "Analyse cet appel. Réponds uniquement par le JSON."
    )

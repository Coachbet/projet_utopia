# Évaluation de scénarios VocalGuard

MOTS-CLÉS SEULS : aucun modèle de langage sollicité.

Mode : `keywords` · Modèle : `none`
Scénarios exploitables : 22/22

Les résultats portent sur des conversations synthétiques, sans téléphone, AWS Transcribe,
signal voix ni Twilio Lookup. Tous les transcripts sont analysés ; le passage direct des
contacts de confiance n'est pas testé. Un petit jeu écrit à la main n'est pas une mesure
de performance sur la population. Les échecs du modèle sont exclus des taux.
Chaque segment synthétique est soumis au modèle avec un intervalle minimal de 0 s ;
cette cadence diffère de celle des appels en production. Le moment de première alerte
suit l'horloge du scénario et ne mesure pas la latence réelle de détection.
Le gate paiement + identité exige un risque de contenu ≥0.65, une tactique de paiement
inhabituel et une identité discordante. Ce seuil de MVP reste heuristique, non calibré.

| Vérité écrite | Alerte déclenchée | Pas d'alerte |
|---|---:|---:|
| Arnaque | 0 | 11 |
| Légitime | 0 | 11 |

Sensibilité : 0.0 · Spécificité : 1.0
Précision : None · Exactitude : 0.5

Latences mesurées du signal de contenu (ms), incluant les appels échoués :
{"samples": 0, "p50": null, "p95": null, "max": null}

| Scénario | Langue | Vérité | Statut | Route réelle | Pic | Décision |
|---|---|---|---|---|---:|---|
| legit_bank_callback | fr | legitimate | evaluated | analyzed | 40 | legitimate |
| legit_card_safety_education | fr | legitimate | evaluated | analyzed | 40 | legitimate |
| legit_clinic_urgent | fr | legitimate | evaluated | analyzed | 8 | legitimate |
| legit_delivery_schedule | fr | legitimate | evaluated | analyzed | 5 | legitimate |
| legit_invoice_plumber | fr | legitimate | evaluated | analyzed | 13 | legitimate |
| legit_neighbor_mixed | fr-en | legitimate | evaluated | analyzed | 5 | legitimate |
| legit_tourist_english | en | legitimate | evaluated | analyzed | 5 | legitimate |
| legit_urgent_borrowed_phone | fr | legitimate | evaluated | analyzed | 11 | legitimate |
| scam_bank_mixed | fr-en | scam | evaluated | analyzed | 11 | legitimate |
| scam_delivery_otp | fr | scam | evaluated | analyzed | 40 | legitimate |
| scam_family_english | en | scam | evaluated | analyzed | 5 | legitimate |
| scam_insurance_refund | fr | scam | evaluated | analyzed | 40 | legitimate |
| scam_invest_crypto | fr | scam | evaluated | analyzed | 40 | legitimate |
| scam_known_identity | fr | scam | evaluated | trusted_passthrough | 40 | legitimate |
| scam_police_english | en | scam | evaluated | analyzed | 40 | legitimate |
| scam_support_remote | fr | scam | evaluated | analyzed | 40 | legitimate |
| legit_01_famille | fr | legitimate | evaluated | trusted_passthrough | 2 | legitimate |
| legit_02_fille_urgence_reelle | fr | legitimate | evaluated | trusted_passthrough | 4 | legitimate |
| legit_03_vrai_conseiller_bancaire | fr | legitimate | evaluated | analyzed | 40 | legitimate |
| scam_01_petit_fils | fr | scam | evaluated | analyzed | 40 | legitimate |
| scam_02_faux_conseiller_bancaire | fr | scam | evaluated | analyzed | 40 | legitimate |
| scam_03_fausse_autorite | fr | scam | evaluated | analyzed | 40 | legitimate |

## Appels dont la route active l'analyse

| Vérité écrite | Alerte déclenchée | Pas d'alerte |
|---|---:|---:|
| Arnaque | 0 | 10 |
| Légitime | 0 | 9 |

## Projection du routage réel (aucune téléphonie testée)

| Vérité écrite | Alerte déclenchée | Pas d'alerte |
|---|---:|---:|
| Arnaque | 0 | 11 |
| Légitime | 0 | 11 |

Projection du routage, sans appel réel : un contact de confiance passe sans alerte ni analyse. La vérification manuelle est hors périmètre.

Le cas d'usurpation du numéro connu est donc un angle mort du passage direct,
même si son texte aurait été reconnu par un analyseur dans ce banc synthétique.

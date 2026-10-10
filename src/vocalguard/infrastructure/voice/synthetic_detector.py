"""Détection de voix synthétique par wav2vec2 (signal 3 sur 4).

Indice, jamais verdict : le coupe-circuit 4 de `domain/scoring/circuit_breakers`
plafonne le score à 64 quand la voix est le seul signal qui parle, parce que les
modèles publiés généralisent mal sur de l'audio téléphonique 8 kHz. Ce module ne
compense donc pas cette incertitude en gonflant la probabilité : il renvoie ce
que le modèle dit, lissé, ou rien du tout.

« Rien du tout » (`None`) est la dégradation voulue partout où quelque chose
manque — torch absent, modèle non téléchargeable, étiquettes illisibles. Un
risque inventé contaminerait le score des trois autres signaux.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

import numpy as np

from vocalguard.config import get_settings
from vocalguard.domain.events import Speaker
from vocalguard.domain.models import VoiceSignal
from vocalguard.infrastructure.voice.resample import (
    PCM16_BYTES_PER_SAMPLE,
    prepare_window,
)

logger = logging.getLogger(__name__)

SMOOTHING_WINDOWS = 5
"""Fenêtres conservées pour la moyenne. 5 x 4 s = 20 s de contexte : assez pour
qu'une fenêtre aberrante ne fasse pas de pic, assez court pour que le début de
l'appel ne fige pas le signal jusqu'à la fin."""

MAX_BUFFERED_WINDOWS = 2
"""L'inférence CPU peut être plus lente que le temps réel. Au-delà, on jette le
plus ancien : analyser de l'audio périmé n'aide pas à protéger la victime
maintenant."""

_FAKE_MARKERS = ("fake", "spoof", "synthetic", "generated", "clone")
_REAL_MARKERS = ("real", "bona", "genuine", "human", "authentic", "orig")
_NEGATIONS = ("not", "non", "no")


@runtime_checkable
class AudioClassifier(Protocol):
    """Classifieur binaire déjà chargé. Le point d'injection des tests.

    `id2label` reproduit `model.config.id2label` ; `__call__` renvoie les
    probabilités dans l'ordre des identifiants d'étiquette.
    """

    @property
    def id2label(self) -> Mapping[int, str]: ...

    def __call__(self, samples: np.ndarray, sample_rate: int) -> Sequence[float]: ...


def resolve_fake_index(id2label: Mapping[int, str]) -> int | None:
    """Trouve l'identifiant de la classe « synthétique ».

    L'ordre et le nom des étiquettes changent d'une fiche de modèle à l'autre :
    coder en dur l'indice 1 inverserait l'axe sur la moitié des modèles, et un
    signal inversé est pire qu'un signal absent. On exclut d'abord ce qui
    ressemble à la classe authentique (et les formes niées comme `not_fake`),
    puis on cherche un marqueur de synthèse. Sans correspondance, l'appelant
    doit renoncer, pas deviner.
    """
    for index, raw in sorted(id2label.items()):
        normalized = raw.strip().casefold().replace("-", "_").replace(" ", "_")
        if any(marker in normalized for marker in _REAL_MARKERS):
            continue
        if normalized.split("_")[0] in _NEGATIONS:
            continue
        if any(marker in normalized for marker in _FAKE_MARKERS):
            return index
    return None


def _load_classifier(model_id: str) -> AudioClassifier:
    """Charge le modèle Hugging Face. Bloquant : à appeler dans un thread.

    Les imports sont ici et pas en tête de module : torch et transformers sont
    un extra (`make install-voice`), le reste du pipeline doit tourner sans eux.
    """
    import torch
    from transformers import AutoFeatureExtractor, AutoModelForAudioClassification

    extractor = AutoFeatureExtractor.from_pretrained(model_id)
    model = AutoModelForAudioClassification.from_pretrained(model_id)
    model.eval()

    class _TransformersClassifier:
        @property
        def id2label(self) -> Mapping[int, str]:
            return {int(k): str(v) for k, v in (model.config.id2label or {}).items()}

        def __call__(self, samples: np.ndarray, sample_rate: int) -> Sequence[float]:
            inputs = extractor(samples, sampling_rate=sample_rate, return_tensors="pt")
            with torch.no_grad():
                logits = model(**inputs).logits[0]
            return torch.softmax(logits, dim=-1).tolist()

    return _TransformersClassifier()


class Wav2VecVoiceDetector:
    """Implémente le port `VoiceDetector`.

    Seule la piste `caller` est analysée : la voix de la victime n'est pas
    suspecte, et l'ignorer divise par deux le travail CPU.
    """

    def __init__(
        self,
        *,
        classifier: AudioClassifier | None = None,
        model_id: str | None = None,
        window_seconds: float | None = None,
        target_sample_rate: int | None = None,
        enabled: bool | None = None,
        smoothing_windows: int = SMOOTHING_WINDOWS,
    ) -> None:
        settings = get_settings()
        self._model_id = model_id or settings.voice_detector_model_id
        self._window_seconds = (
            window_seconds if window_seconds is not None else settings.voice_detector_window_seconds
        )
        self._target_rate = (
            target_sample_rate
            if target_sample_rate is not None
            else settings.voice_detector_target_sample_rate
        )
        # Injecter un classifieur vaut intention explicite : on n'exige pas en
        # plus le réglage d'environnement, sinon les tests ne verraient jamais
        # le chemin d'inférence.
        if enabled is None:
            enabled = True if classifier is not None else settings.voice_detector_enabled

        self._classifier = classifier
        self._fake_index: int | None = None
        self._buffer = bytearray()
        self._recent: deque[float] = deque(maxlen=max(1, smoothing_windows))
        self._windows_analyzed = 0
        self._lock = asyncio.Lock()
        self._dead = not enabled
        self._death_logged = False

        if self._dead:
            logger.info("Détection de voix synthétique désactivée (VOICE_DETECTOR_ENABLED).")
        elif classifier is not None:
            self._fake_index = resolve_fake_index(classifier.id2label)
            if self._fake_index is None:
                self._die(
                    "étiquettes du classifieur injecté non reconnues : %s", classifier.id2label
                )

    def _die(self, message: str, *args: object) -> None:
        """Éteint le signal définitivement, en ne le disant qu'une fois."""
        self._dead = True
        self._buffer.clear()
        if not self._death_logged:
            self._death_logged = True
            logger.warning(
                "Signal voix synthétique indisponible, ignoré pour tout l'appel. " + message,
                *args,
            )

    async def feed(self, speaker: Speaker, pcm16: bytes, sample_rate: int) -> VoiceSignal | None:
        if self._dead or speaker != "caller" or not pcm16:
            return None

        window_bytes = int(self._window_seconds * sample_rate) * PCM16_BYTES_PER_SAMPLE
        if window_bytes <= 0:
            return None

        self._buffer.extend(pcm16)
        overflow = len(self._buffer) - window_bytes * MAX_BUFFERED_WINDOWS
        if overflow > 0:
            del self._buffer[:overflow]

        if len(self._buffer) < window_bytes:
            return None
        if self._lock.locked():
            # Inférence déjà en cours : on accumule sans empiler les fenêtres,
            # la plus récente sera analysée au prochain passage.
            return None

        async with self._lock:
            if len(self._buffer) < window_bytes:
                return None
            window = bytes(self._buffer[:window_bytes])
            del self._buffer[:window_bytes]
            probability = await asyncio.to_thread(self._infer, window, sample_rate)

        if probability is None:
            return None

        self._windows_analyzed += 1
        self._recent.append(probability)
        return VoiceSignal(
            risk=float(sum(self._recent) / len(self._recent)),
            windows_analyzed=self._windows_analyzed,
        )

    def _infer(self, window: bytes, sample_rate: int) -> float | None:
        """Chargement paresseux puis inférence. Tourne dans un thread."""
        if self._classifier is None:
            try:
                self._classifier = _load_classifier(self._model_id)
            except ImportError as exc:
                self._die("torch/transformers absents (make install-voice) : %s", exc)
                return None
            except Exception as exc:  # réseau du lieu, modèle introuvable, disque…
                self._die("chargement de %s impossible : %s", self._model_id, exc)
                return None
            self._fake_index = resolve_fake_index(self._classifier.id2label)
            if self._fake_index is None:
                self._die(
                    "aucune étiquette « synthétique » dans %s : %s",
                    self._model_id,
                    self._classifier.id2label,
                )
                return None

        try:
            samples = prepare_window(window, sample_rate, self._target_rate)
            probabilities = self._classifier(samples, self._target_rate)
            return float(probabilities[self._fake_index])
        except Exception as exc:
            self._die("inférence en échec : %s", exc)
            return None

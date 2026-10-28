"""Speech engines: text to speech, speech to text and the sentence splitter. No knowledge of channels, apps or the
core, and it imports nothing else from jarvis."""

from jarvis.speech.listener import WhisperListener
from jarvis.speech.sentences import SentenceBuffer, sentences
from jarvis.speech.types import SAMPLE_RATE, Listener, Voice, VoiceProfile
from jarvis.speech.voice import ENGINES, VoiceManager

__all__ = ["ENGINES", "SAMPLE_RATE", "Listener", "SentenceBuffer", "Voice", "VoiceManager", "VoiceProfile",
           "WhisperListener", "sentences"]

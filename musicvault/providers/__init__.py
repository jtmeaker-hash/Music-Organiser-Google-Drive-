"""
Metadata providers package.
"""
from .base import BaseProvider
from .sonovault import SonoVaultProvider
from .audiometa import AudioMetaProvider
from .discogs import DiscogsProvider
from .hdrr import HDRRProvider
from .musicbrainz import MusicBrainzProvider
from .research import ResearchProvider

__all__ = [
    "BaseProvider",
    "SonoVaultProvider",
    "AudioMetaProvider",
    "DiscogsProvider",
    "HDRRProvider",
    "MusicBrainzProvider",
    "ResearchProvider",
]

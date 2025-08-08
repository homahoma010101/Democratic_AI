"""
Democratic AI Python SDK
Easy-to-use SDK for developing AI modules for the Democratic AI platform
"""

__version__ = "2.0.0"

from .module import Module, capability, stream_capable
from .client import DemocraticAIClient
from .exceptions import DemocraticAIError, ValidationError, ConnectionError
from .types import Context, Capability, StreamData

__all__ = [
    "Module",
    "capability",
    "stream_capable",
    "DemocraticAIClient",
    "DemocraticAIError",
    "ValidationError",
    "ConnectionError",
    "Context",
    "Capability",
    "StreamData"
]

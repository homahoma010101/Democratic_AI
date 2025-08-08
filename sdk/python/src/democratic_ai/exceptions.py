"""
Exception definitions for Democratic AI SDK
"""


class DemocraticAIError(Exception):
    """Base exception for Democratic AI SDK"""
    pass


class ValidationError(DemocraticAIError):
    """Raised when input/output validation fails"""
    pass


class ConnectionError(DemocraticAIError):
    """Raised when connection to hub fails"""
    pass


class AuthenticationError(DemocraticAIError):
    """Raised when authentication fails"""
    pass


class TimeoutError(DemocraticAIError):
    """Raised when a request times out"""
    pass


class RateLimitError(DemocraticAIError):
    """Raised when rate limit is exceeded"""
    pass


class ModuleNotFoundError(DemocraticAIError):
    """Raised when a module is not found"""
    pass


class CapabilityNotFoundError(DemocraticAIError):
    """Raised when a capability is not found"""
    pass

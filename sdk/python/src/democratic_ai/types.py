"""
Type definitions for Democratic AI SDK
"""

from dataclasses import dataclass
from typing import Dict, Any, List, Optional
from datetime import datetime


@dataclass
class Context:
    """Request context information"""
    tenant_id: str
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    timestamp: Optional[datetime] = None
    metadata: Dict[str, Any] = None
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Context':
        """Create Context from dictionary"""
        return cls(
            tenant_id=data.get('tenant_id', ''),
            user_id=data.get('user_id'),
            session_id=data.get('session_id'),
            trace_id=data.get('trace_id'),
            span_id=data.get('span_id'),
            timestamp=datetime.fromisoformat(data['timestamp']) if 'timestamp' in data else None,
            metadata=data.get('metadata', {})
        )


@dataclass
class Capability:
    """Module capability definition"""
    action: str
    input_schema: Dict[str, Any]
    output_schema: Dict[str, Any]
    description: str = ""
    tags: List[str] = None
    cost_per_call: float = 0.0
    handler: Optional[Any] = None  # The actual function handler
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            'action': self.action,
            'input_schema': self.input_schema,
            'output_schema': self.output_schema,
            'description': self.description,
            'tags': self.tags or [],
            'cost_per_call': self.cost_per_call
        }


@dataclass
class StreamData:
    """Data for streaming responses"""
    data: Any
    progress: float = 0.0
    is_final: bool = False
    metadata: Dict[str, Any] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            'data': self.data,
            'progress': self.progress,
            'is_final': self.is_final,
            'metadata': self.metadata or {}
        }

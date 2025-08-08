"""
Module base class and decorators for Democratic AI
"""

import asyncio
import json
import uuid
import functools
import inspect
from typing import Dict, Any, Callable, Optional, List, AsyncGenerator
from dataclasses import dataclass
import websockets
import jsonschema
import logging

from .exceptions import ValidationError, ConnectionError
from .types import Context, Capability, StreamData

logger = logging.getLogger(__name__)


def capability(
    action: str,
    input_schema: Dict[str, Any],
    output_schema: Dict[str, Any],
    description: str = "",
    tags: List[str] = None,
    cost_per_call: float = 0.0
):
    """
    Decorator to define a module capability
    
    Args:
        action: The action identifier (e.g., "text.summarize")
        input_schema: JSON Schema for input validation
        output_schema: JSON Schema for output validation
        description: Human-readable description of the capability
        tags: Optional tags for categorization
        cost_per_call: Optional cost in credits per call
    
    Example:
        @capability(
            action="text.summarize",
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "minLength": 50},
                    "max_length": {"type": "integer", "minimum": 10, "maximum": 500}
                },
                "required": ["text"]
            },
            output_schema={
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "compression_ratio": {"type": "number"}
                }
            },
            description="Summarizes long text into concise summaries"
        )
        async def summarize(self, text: str, max_length: int = 100):
            # Implementation
            summary = text[:max_length]  # Simplified
            return {
                "summary": summary,
                "compression_ratio": len(summary) / len(text)
            }
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(self, message: Dict[str, Any]) -> Dict[str, Any]:
            payload = message.get('payload', {})
            context = Context.from_dict(message.get('context', {}))
            
            # Input validation
            try:
                jsonschema.validate(payload, input_schema)
            except jsonschema.ValidationError as e:
                raise ValidationError(f"Input validation failed: {e.message}")
            
            # Call the actual function
            if inspect.iscoroutinefunction(func):
                result = await func(self, context=context, **payload)
            else:
                result = func(self, context=context, **payload)
            
            # Output validation
            try:
                jsonschema.validate(result, output_schema)
            except jsonschema.ValidationError as e:
                raise ValidationError(f"Output validation failed: {e.message}")
            
            return result
        
        # Store capability metadata
        wrapper._capability = Capability(
            action=action,
            input_schema=input_schema,
            output_schema=output_schema,
            description=description,
            tags=tags or [],
            cost_per_call=cost_per_call
        )
        
        return wrapper
    
    return decorator


def stream_capable(func: Callable) -> Callable:
    """
    Decorator to mark a capability as stream-capable
    
    Example:
        @capability(...)
        @stream_capable
        async def generate_text(self, prompt: str, context: Context):
            for i in range(10):
                yield StreamData(
                    data=f"Part {i}",
                    progress=i/10,
                    is_final=i==9
                )
    """
    @functools.wraps(func)
    async def wrapper(self, *args, **kwargs):
        async for item in func(self, *args, **kwargs):
            yield item
    
    wrapper._stream_capable = True
    return wrapper


class Module:
    """
    Base class for Democratic AI modules
    
    Example:
        class TextAnalyzer(Module):
            def __init__(self):
                super().__init__(
                    module_id="text-analyzer",
                    name="Text Analyzer",
                    version="1.0.0"
                )
                
            @capability(...)
            async def analyze(self, text: str, context: Context):
                # Your implementation
                return {"result": "analysis"}
    """
    
    def __init__(
        self,
        module_id: str,
        name: str,
        version: str = "1.0.0",
        description: str = "",
        metadata: Dict[str, Any] = None
    ):
        self.module_id = module_id
        self.name = name
        self.version = version
        self.description = description
        self.metadata = metadata or {}
        
        # Connection state
        self.websocket = None
        self.connected = False
        self.hub_url = None
        
        # Discover capabilities
        self.capabilities = self._discover_capabilities()
        
        # Message handlers
        self.message_handlers = {}
        
        # Metrics
        self.metrics = {
            'total_requests': 0,
            'total_errors': 0,
            'total_time': 0.0
        }
        
    def _discover_capabilities(self) -> List[Capability]:
        """Discover capabilities from decorated methods"""
        capabilities = []
        
        for name, method in inspect.getmembers(self, predicate=inspect.ismethod):
            if hasattr(method, '_capability'):
                cap = method._capability
                cap.handler = method
                capabilities.append(cap)
                
        return capabilities
        
    async def connect(self, hub_url: str, auth_token: Optional[str] = None):
        """
        Connect to the MCP Hub
        
        Args:
            hub_url: WebSocket URL of the MCP Hub
            auth_token: Optional authentication token
        """
        self.hub_url = hub_url
        
        try:
            self.websocket = await websockets.connect(hub_url)
            
            # Send authentication/registration message
            auth_message = {
                'module_id': self.module_id,
                'name': self.name,
                'version': self.version,
                'capabilities': [cap.action for cap in self.capabilities],
                'metadata': self.metadata
            }
            
            if auth_token:
                auth_message['auth_token'] = auth_token
                
            await self.websocket.send(json.dumps(auth_message))
            
            # Wait for acknowledgment
            response = await self.websocket.recv()
            response_data = json.loads(response)
            
            if response_data.get('type') == 'connected':
                self.connected = True
                logger.info(f"Module {self.module_id} connected to hub")
                
                # Start message processing loop
                asyncio.create_task(self._process_messages())
            else:
                raise ConnectionError(f"Failed to connect: {response_data.get('message')}")
                
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            raise ConnectionError(f"Failed to connect to hub: {e}")
            
    async def disconnect(self):
        """Disconnect from the MCP Hub"""
        if self.websocket:
            await self.websocket.close()
            self.connected = False
            logger.info(f"Module {self.module_id} disconnected from hub")
            
    async def _process_messages(self):
        """Process incoming messages from the hub"""
        try:
            async for message in self.websocket:
                try:
                    data = json.loads(message)
                    await self._handle_message(data)
                except json.JSONDecodeError as e:
                    logger.error(f"Invalid JSON received: {e}")
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
                    
        except websockets.exceptions.ConnectionClosed:
            logger.info("Connection to hub closed")
            self.connected = False
            
    async def _handle_message(self, message: Dict[str, Any]):
        """Handle incoming message"""
        msg_type = message.get('type')
        
        if msg_type == 'REQUEST':
            await self._handle_request(message)
        elif msg_type == 'HEARTBEAT':
            await self._handle_heartbeat(message)
        elif msg_type == 'CAPABILITY_QUERY':
            await self._handle_capability_query(message)
        else:
            logger.warning(f"Unknown message type: {msg_type}")
            
    async def _handle_request(self, message: Dict[str, Any]):
        """Handle execution request"""
        request_id = message.get('id')
        action = message.get('action')
        correlation_id = message.get('correlation_id')
        
        self.metrics['total_requests'] += 1
        start_time = asyncio.get_event_loop().time()
        
        try:
            # Find capability handler
            capability = None
            for cap in self.capabilities:
                if cap.action == action:
                    capability = cap
                    break
                    
            if not capability:
                raise ValueError(f"Unknown action: {action}")
                
            # Execute handler
            result = await capability.handler(message)
            
            # Send response
            response = {
                'id': str(uuid.uuid4()),
                'type': 'RESPONSE',
                'source_module_id': self.module_id,
                'target_module_id': message.get('source_module_id'),
                'action': action,
                'correlation_id': correlation_id,
                'payload': result,
                'timestamp': asyncio.get_event_loop().time()
            }
            
            await self.websocket.send(json.dumps(response))
            
            # Update metrics
            self.metrics['total_time'] += asyncio.get_event_loop().time() - start_time
            
        except Exception as e:
            self.metrics['total_errors'] += 1
            logger.error(f"Error handling request {request_id}: {e}")
            
            # Send error response
            error_response = {
                'id': str(uuid.uuid4()),
                'type': 'ERROR',
                'source_module_id': self.module_id,
                'target_module_id': message.get('source_module_id'),
                'correlation_id': correlation_id,
                'error': {
                    'code': 'EXECUTION_ERROR',
                    'message': str(e)
                },
                'timestamp': asyncio.get_event_loop().time()
            }
            
            await self.websocket.send(json.dumps(error_response))
            
    async def _handle_heartbeat(self, message: Dict[str, Any]):
        """Handle heartbeat message"""
        response = {
            'type': 'HEARTBEAT_ACK',
            'module_id': self.module_id,
            'timestamp': asyncio.get_event_loop().time()
        }
        await self.websocket.send(json.dumps(response))
        
    async def _handle_capability_query(self, message: Dict[str, Any]):
        """Handle capability query"""
        response = {
            'type': 'CAPABILITY_RESPONSE',
            'module_id': self.module_id,
            'capabilities': [
                {
                    'action': cap.action,
                    'description': cap.description,
                    'input_schema': cap.input_schema,
                    'output_schema': cap.output_schema,
                    'tags': cap.tags,
                    'stream_capable': hasattr(cap.handler, '_stream_capable')
                }
                for cap in self.capabilities
            ]
        }
        await self.websocket.send(json.dumps(response))
        
    async def emit_event(self, event_type: str, data: Any):
        """
        Emit an event to the hub
        
        Args:
            event_type: Type of event
            data: Event data
        """
        if not self.connected:
            raise ConnectionError("Not connected to hub")
            
        event = {
            'id': str(uuid.uuid4()),
            'type': 'EVENT',
            'source_module_id': self.module_id,
            'event_type': event_type,
            'payload': data,
            'timestamp': asyncio.get_event_loop().time()
        }
        
        await self.websocket.send(json.dumps(event))
        
    async def call_module(
        self,
        target_module: str,
        action: str,
        payload: Dict[str, Any],
        timeout: int = 30
    ) -> Dict[str, Any]:
        """
        Call another module through the hub
        
        Args:
            target_module: Target module ID
            action: Action to execute
            payload: Request payload
            timeout: Request timeout in seconds
            
        Returns:
            Response from the target module
        """
        if not self.connected:
            raise ConnectionError("Not connected to hub")
            
        request_id = str(uuid.uuid4())
        correlation_id = str(uuid.uuid4())
        
        request = {
            'id': request_id,
            'type': 'REQUEST',
            'source_module_id': self.module_id,
            'target_module_id': target_module,
            'action': action,
            'payload': payload,
            'correlation_id': correlation_id,
            'timestamp': asyncio.get_event_loop().time(),
            'ttl': timeout
        }
        
        # Create future for response
        response_future = asyncio.Future()
        self.message_handlers[correlation_id] = response_future
        
        # Send request
        await self.websocket.send(json.dumps(request))
        
        try:
            # Wait for response
            response = await asyncio.wait_for(response_future, timeout=timeout)
            return response
        except asyncio.TimeoutError:
            raise TimeoutError(f"Request to {target_module} timed out")
        finally:
            # Clean up handler
            if correlation_id in self.message_handlers:
                del self.message_handlers[correlation_id]
                
    def get_metrics(self) -> Dict[str, Any]:
        """Get module metrics"""
        return {
            'module_id': self.module_id,
            'total_requests': self.metrics['total_requests'],
            'total_errors': self.metrics['total_errors'],
            'average_response_time': (
                self.metrics['total_time'] / self.metrics['total_requests']
                if self.metrics['total_requests'] > 0 else 0
            ),
            'error_rate': (
                self.metrics['total_errors'] / self.metrics['total_requests']
                if self.metrics['total_requests'] > 0 else 0
            )
        }
        
    async def run(self, hub_url: str, auth_token: Optional[str] = None):
        """
        Run the module (connect and process messages)
        
        Args:
            hub_url: WebSocket URL of the MCP Hub
            auth_token: Optional authentication token
        """
        await self.connect(hub_url, auth_token)
        
        try:
            # Keep running until interrupted
            while self.connected:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            logger.info("Module interrupted")
        finally:
            await self.disconnect()

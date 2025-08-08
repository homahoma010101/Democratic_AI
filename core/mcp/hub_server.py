"""
MCP Hub Server - Central message routing system for Democratic AI
Handles WebSocket, gRPC, and HTTP/3 connections
"""

import asyncio
import json
import uuid
import time
import logging
from typing import Dict, Any, Set, Optional, List
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timedelta
import hashlib
from collections import defaultdict

import websockets
from websockets.server import WebSocketServerProtocol
import grpc
from concurrent import futures

# Logging configuration
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class Protocol(Enum):
    """Supported communication protocols"""
    WEBSOCKET = "websocket"
    GRPC = "grpc"
    HTTP3 = "http3"


class MessageType(Enum):
    """MCP message types"""
    REQUEST = "REQUEST"
    RESPONSE = "RESPONSE"
    EVENT = "EVENT"
    STREAM_START = "STREAM_START"
    STREAM_DATA = "STREAM_DATA"
    STREAM_END = "STREAM_END"
    ERROR = "ERROR"
    HEARTBEAT = "HEARTBEAT"
    CAPABILITY_ANNOUNCE = "CAPABILITY_ANNOUNCE"
    CAPABILITY_QUERY = "CAPABILITY_QUERY"


class RoutingStrategy(Enum):
    """Message routing strategies"""
    DIRECT = "direct"            # Route to specific module
    CAPABILITY = "capability"    # Route based on capability
    BROADCAST = "broadcast"      # Broadcast to all modules
    ROUND_ROBIN = "round_robin"  # Load balancing
    LEAST_LOADED = "least_loaded"  # Route to least loaded module
    WEIGHTED = "weighted"        # Weighted distribution


@dataclass
class ModuleConnection:
    """Represents a connected module"""
    module_id: str
    protocol: Protocol
    connection: Any  # WebSocket, gRPC channel, etc.
    capabilities: Set[str] = field(default_factory=set)
    status: str = "online"
    connected_at: datetime = field(default_factory=datetime.utcnow)
    last_heartbeat: datetime = field(default_factory=datetime.utcnow)
    request_count: int = 0
    error_count: int = 0
    average_response_time: float = 0.0
    current_load: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RoutingDecision:
    """Routing decision for a message"""
    strategy: RoutingStrategy
    target_modules: List[str]
    priority: int
    reason: str


class MCPHub:
    """Main MCP Hub server implementation"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.modules: Dict[str, ModuleConnection] = {}
        self.capabilities_index: Dict[str, Set[str]] = defaultdict(set)
        self.message_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self.routing_cache: Dict[str, str] = {}
        self.stream_managers: Dict[str, 'StreamManager'] = {}
        
        # Metrics
        self.total_messages = 0
        self.total_errors = 0
        self.message_latencies = []
        
        # WebSocket server
        self.websocket_server = None
        self.websocket_connections: Dict[str, WebSocketServerProtocol] = {}
        
        # gRPC server
        self.grpc_server = None
        
        # Configuration
        self.heartbeat_interval = config.get('heartbeat_interval', 30)
        self.heartbeat_timeout = config.get('heartbeat_timeout', 60)
        self.max_message_size = config.get('max_message_size', 10 * 1024 * 1024)  # 10MB
        self.enable_message_compression = config.get('enable_compression', True)
        
    async def start(self):
        """Start the MCP Hub server"""
        logger.info("Starting MCP Hub Server...")
        
        # Start WebSocket server
        if self.config.get('enable_websocket', True):
            await self._start_websocket_server()
            
        # Start gRPC server
        if self.config.get('enable_grpc', True):
            await self._start_grpc_server()
            
        # Start background tasks
        asyncio.create_task(self._heartbeat_monitor())
        asyncio.create_task(self._process_message_queue())
        asyncio.create_task(self._cleanup_stale_connections())
        asyncio.create_task(self._metrics_reporter())
        
        logger.info("MCP Hub Server started successfully")
        
    async def _start_websocket_server(self):
        """Start WebSocket server"""
        host = self.config.get('websocket_host', '0.0.0.0')
        port = self.config.get('websocket_port', 8765)
        
        self.websocket_server = await websockets.serve(
            self._handle_websocket_connection,
            host, port,
            max_size=self.max_message_size,
            compression='deflate' if self.enable_message_compression else None
        )
        
        logger.info(f"WebSocket server listening on {host}:{port}")
        
    async def _start_grpc_server(self):
        """Start gRPC server"""
        # gRPC implementation would go here
        # This is a placeholder for the actual gRPC server setup
        pass
        
    async def _handle_websocket_connection(self, websocket: WebSocketServerProtocol, path: str):
        """Handle new WebSocket connection"""
        connection_id = str(uuid.uuid4())
        logger.info(f"New WebSocket connection: {connection_id} from {websocket.remote_address}")
        
        try:
            # Authentication phase
            auth_msg = await asyncio.wait_for(websocket.recv(), timeout=10)
            auth_data = json.loads(auth_msg)
            
            if not await self._authenticate_module(auth_data):
                await websocket.send(json.dumps({
                    'type': 'error',
                    'code': 'AUTH_FAILED',
                    'message': 'Authentication failed'
                }))
                return
                
            module_id = auth_data.get('module_id')
            capabilities = set(auth_data.get('capabilities', []))
            
            # Register module connection
            connection = ModuleConnection(
                module_id=module_id,
                protocol=Protocol.WEBSOCKET,
                connection=websocket,
                capabilities=capabilities
            )
            
            self.modules[module_id] = connection
            self.websocket_connections[connection_id] = websocket
            
            # Update capability index
            for capability in capabilities:
                self.capabilities_index[capability].add(module_id)
                
            # Send acknowledgment
            await websocket.send(json.dumps({
                'type': 'connected',
                'module_id': module_id,
                'hub_version': '2.0.0'
            }))
            
            # Handle messages
            async for message in websocket:
                try:
                    msg_data = json.loads(message)
                    await self._handle_message(msg_data, module_id, Protocol.WEBSOCKET)
                except json.JSONDecodeError as e:
                    logger.error(f"Invalid JSON from {module_id}: {e}")
                    await self._send_error(module_id, "INVALID_JSON", str(e))
                except Exception as e:
                    logger.error(f"Error handling message from {module_id}: {e}")
                    await self._send_error(module_id, "MESSAGE_PROCESSING_ERROR", str(e))
                    
        except asyncio.TimeoutError:
            logger.warning(f"Authentication timeout for connection {connection_id}")
        except Exception as e:
            logger.error(f"Connection error for {connection_id}: {e}")
        finally:
            # Cleanup
            if module_id and module_id in self.modules:
                await self._unregister_module(module_id)
            if connection_id in self.websocket_connections:
                del self.websocket_connections[connection_id]
                
    async def _authenticate_module(self, auth_data: Dict[str, Any]) -> bool:
        """Authenticate a module connection"""
        # TODO: Implement actual authentication logic
        # For now, just check if module_id is provided
        return bool(auth_data.get('module_id'))
        
    async def _handle_message(self, message: Dict[str, Any], source_module_id: str, protocol: Protocol):
        """Handle incoming message from a module"""
        self.total_messages += 1
        start_time = time.time()
        
        try:
            # Validate message
            if not self._validate_message(message):
                await self._send_error(source_module_id, "INVALID_MESSAGE", "Message validation failed")
                return
                
            # Update module metrics
            if source_module_id in self.modules:
                self.modules[source_module_id].request_count += 1
                self.modules[source_module_id].last_heartbeat = datetime.utcnow()
                
            # Determine message type
            msg_type = MessageType(message.get('type', 'REQUEST'))
            
            if msg_type == MessageType.HEARTBEAT:
                await self._handle_heartbeat(source_module_id)
            elif msg_type == MessageType.CAPABILITY_ANNOUNCE:
                await self._handle_capability_announce(source_module_id, message)
            elif msg_type == MessageType.CAPABILITY_QUERY:
                await self._handle_capability_query(source_module_id, message)
            elif msg_type in [MessageType.STREAM_START, MessageType.STREAM_DATA, MessageType.STREAM_END]:
                await self._handle_stream_message(source_module_id, message)
            else:
                await self._route_message(message, source_module_id)
                
            # Record latency
            latency = (time.time() - start_time) * 1000  # Convert to ms
            self.message_latencies.append(latency)
            
        except Exception as e:
            self.total_errors += 1
            logger.error(f"Error processing message from {source_module_id}: {e}")
            await self._send_error(source_module_id, "PROCESSING_ERROR", str(e))
            
    async def _route_message(self, message: Dict[str, Any], source_module_id: str):
        """Route a message to appropriate target(s)"""
        target_module_id = message.get('target_module_id')
        action = message.get('action')
        priority = message.get('priority', 5)
        
        # Determine routing strategy
        routing_decision = self._determine_routing(target_module_id, action, message)
        
        if routing_decision.strategy == RoutingStrategy.DIRECT:
            # Direct routing to specific module
            if target_module_id in self.modules:
                await self._send_to_module(target_module_id, message)
            else:
                await self._send_error(source_module_id, "MODULE_NOT_FOUND", 
                                     f"Target module {target_module_id} not found")
                
        elif routing_decision.strategy == RoutingStrategy.CAPABILITY:
            # Route based on capability
            if action in self.routing_cache:
                # Use cached routing
                cached_module_id = self.routing_cache[action]
                if cached_module_id in self.modules:
                    await self._send_to_module(cached_module_id, message)
                    return
                    
            # Find modules with the capability
            target_modules = self._find_modules_for_action(action)
            
            if target_modules:
                # Select best module based on load balancing
                selected_module = await self._select_module(target_modules, message)
                self.routing_cache[action] = selected_module  # Cache the decision
                await self._send_to_module(selected_module, message)
            else:
                await self._send_error(source_module_id, "NO_CAPABILITY", 
                                     f"No module found with capability: {action}")
                
        elif routing_decision.strategy == RoutingStrategy.BROADCAST:
            # Broadcast to all modules (except source)
            for module_id in self.modules:
                if module_id != source_module_id:
                    await self._send_to_module(module_id, message)
                    
    def _determine_routing(self, target_module_id: Optional[str], action: Optional[str], 
                          message: Dict[str, Any]) -> RoutingDecision:
        """Determine the routing strategy for a message"""
        if target_module_id:
            return RoutingDecision(
                strategy=RoutingStrategy.DIRECT,
                target_modules=[target_module_id],
                priority=message.get('priority', 5),
                reason="Direct routing to specified module"
            )
        elif action:
            return RoutingDecision(
                strategy=RoutingStrategy.CAPABILITY,
                target_modules=list(self.capabilities_index.get(action, [])),
                priority=message.get('priority', 5),
                reason=f"Capability-based routing for action: {action}"
            )
        else:
            return RoutingDecision(
                strategy=RoutingStrategy.BROADCAST,
                target_modules=list(self.modules.keys()),
                priority=message.get('priority', 5),
                reason="Broadcast message"
            )
            
    def _find_modules_for_action(self, action: str) -> List[str]:
        """Find modules that can handle a specific action"""
        return list(self.capabilities_index.get(action, []))
        
    async def _select_module(self, module_ids: List[str], message: Dict[str, Any]) -> str:
        """Select the best module from candidates using load balancing"""
        # Filter online modules
        online_modules = [mid for mid in module_ids if self.modules[mid].status == "online"]
        
        if not online_modules:
            raise ValueError("No online modules available")
            
        # Use least loaded strategy
        min_load = float('inf')
        selected = None
        
        for module_id in online_modules:
            module = self.modules[module_id]
            if module.current_load < min_load:
                min_load = module.current_load
                selected = module_id
                
        return selected
        
    async def _send_to_module(self, module_id: str, message: Dict[str, Any]):
        """Send a message to a specific module"""
        if module_id not in self.modules:
            raise ValueError(f"Module {module_id} not found")
            
        module = self.modules[module_id]
        module.current_load += 1
        
        try:
            if module.protocol == Protocol.WEBSOCKET:
                await module.connection.send(json.dumps(message))
            elif module.protocol == Protocol.GRPC:
                # TODO: Implement gRPC sending
                pass
            elif module.protocol == Protocol.HTTP3:
                # TODO: Implement HTTP/3 sending
                pass
                
            logger.debug(f"Message sent to {module_id}: {message.get('id')}")
            
        except Exception as e:
            logger.error(f"Failed to send message to {module_id}: {e}")
            module.error_count += 1
            raise
        finally:
            module.current_load -= 1
            
    async def _send_error(self, module_id: str, error_code: str, error_message: str):
        """Send an error message to a module"""
        error_msg = {
            'type': 'ERROR',
            'code': error_code,
            'message': error_message,
            'timestamp': datetime.utcnow().isoformat()
        }
        
        try:
            await self._send_to_module(module_id, error_msg)
        except Exception as e:
            logger.error(f"Failed to send error to {module_id}: {e}")
            
    def _validate_message(self, message: Dict[str, Any]) -> bool:
        """Validate message structure"""
        required_fields = ['id', 'type']
        return all(field in message for field in required_fields)
        
    async def _handle_heartbeat(self, module_id: str):
        """Handle heartbeat message from a module"""
        if module_id in self.modules:
            self.modules[module_id].last_heartbeat = datetime.utcnow()
            
            # Send heartbeat response
            response = {
                'type': 'HEARTBEAT_ACK',
                'timestamp': datetime.utcnow().isoformat()
            }
            await self._send_to_module(module_id, response)
            
    async def _handle_capability_announce(self, module_id: str, message: Dict[str, Any]):
        """Handle capability announcement from a module"""
        capabilities = set(message.get('capabilities', []))
        
        if module_id in self.modules:
            # Update module capabilities
            old_capabilities = self.modules[module_id].capabilities
            self.modules[module_id].capabilities = capabilities
            
            # Update capability index
            # Remove old capabilities
            for cap in old_capabilities:
                if cap in self.capabilities_index:
                    self.capabilities_index[cap].discard(module_id)
                    
            # Add new capabilities
            for cap in capabilities:
                self.capabilities_index[cap].add(module_id)
                
            logger.info(f"Updated capabilities for {module_id}: {capabilities}")
            
    async def _handle_capability_query(self, module_id: str, message: Dict[str, Any]):
        """Handle capability query from a module"""
        action = message.get('action')
        
        if action:
            modules = self._find_modules_for_action(action)
            response = {
                'type': 'CAPABILITY_RESPONSE',
                'action': action,
                'modules': modules,
                'timestamp': datetime.utcnow().isoformat()
            }
        else:
            # Return all capabilities
            response = {
                'type': 'CAPABILITY_RESPONSE',
                'capabilities': {
                    action: list(modules)
                    for action, modules in self.capabilities_index.items()
                },
                'timestamp': datetime.utcnow().isoformat()
            }
            
        await self._send_to_module(module_id, response)
        
    async def _handle_stream_message(self, module_id: str, message: Dict[str, Any]):
        """Handle streaming messages"""
        stream_id = message.get('stream_id')
        msg_type = MessageType(message.get('type'))
        
        if msg_type == MessageType.STREAM_START:
            # Initialize stream manager
            self.stream_managers[stream_id] = StreamManager(stream_id, module_id)
            logger.info(f"Stream started: {stream_id} from {module_id}")
            
        elif msg_type == MessageType.STREAM_DATA:
            # Process stream data
            if stream_id in self.stream_managers:
                await self.stream_managers[stream_id].add_data(message.get('data'))
                
        elif msg_type == MessageType.STREAM_END:
            # Finalize stream
            if stream_id in self.stream_managers:
                await self.stream_managers[stream_id].finalize()
                del self.stream_managers[stream_id]
                logger.info(f"Stream ended: {stream_id}")
                
    async def _unregister_module(self, module_id: str):
        """Unregister a module from the hub"""
        if module_id in self.modules:
            module = self.modules[module_id]
            
            # Remove from capability index
            for capability in module.capabilities:
                if capability in self.capabilities_index:
                    self.capabilities_index[capability].discard(module_id)
                    
            # Remove from modules
            del self.modules[module_id]
            
            # Clear routing cache entries
            self.routing_cache = {
                k: v for k, v in self.routing_cache.items() 
                if v != module_id
            }
            
            logger.info(f"Module unregistered: {module_id}")
            
    async def _heartbeat_monitor(self):
        """Monitor module heartbeats and mark inactive modules as offline"""
        while True:
            try:
                current_time = datetime.utcnow()
                timeout_threshold = timedelta(seconds=self.heartbeat_timeout)
                
                for module_id, module in list(self.modules.items()):
                    if current_time - module.last_heartbeat > timeout_threshold:
                        if module.status == "online":
                            module.status = "offline"
                            logger.warning(f"Module {module_id} marked as offline (heartbeat timeout)")
                            
                await asyncio.sleep(self.heartbeat_interval)
                
            except Exception as e:
                logger.error(f"Error in heartbeat monitor: {e}")
                await asyncio.sleep(5)
                
    async def _process_message_queue(self):
        """Process queued messages"""
        while True:
            try:
                if not self.message_queue.empty():
                    priority, message, source_module_id = await self.message_queue.get()
                    await self._route_message(message, source_module_id)
                else:
                    await asyncio.sleep(0.1)
                    
            except Exception as e:
                logger.error(f"Error processing message queue: {e}")
                await asyncio.sleep(1)
                
    async def _cleanup_stale_connections(self):
        """Clean up stale connections periodically"""
        while True:
            try:
                current_time = datetime.utcnow()
                cleanup_threshold = timedelta(minutes=30)
                
                for module_id in list(self.modules.keys()):
                    module = self.modules[module_id]
                    if module.status == "offline" and \
                       current_time - module.last_heartbeat > cleanup_threshold:
                        await self._unregister_module(module_id)
                        logger.info(f"Cleaned up stale connection: {module_id}")
                        
                await asyncio.sleep(300)  # Run every 5 minutes
                
            except Exception as e:
                logger.error(f"Error in cleanup task: {e}")
                await asyncio.sleep(60)
                
    async def _metrics_reporter(self):
        """Report metrics periodically"""
        while True:
            try:
                # Calculate average latency
                avg_latency = sum(self.message_latencies[-100:]) / len(self.message_latencies[-100:]) \
                    if self.message_latencies else 0
                    
                # Log metrics
                logger.info(f"Hub Metrics - Total Messages: {self.total_messages}, "
                          f"Total Errors: {self.total_errors}, "
                          f"Active Modules: {len(self.modules)}, "
                          f"Avg Latency: {avg_latency:.2f}ms")
                          
                # Clear old latency data
                if len(self.message_latencies) > 1000:
                    self.message_latencies = self.message_latencies[-1000:]
                    
                await asyncio.sleep(60)  # Report every minute
                
            except Exception as e:
                logger.error(f"Error in metrics reporter: {e}")
                await asyncio.sleep(60)


class StreamManager:
    """Manages streaming data for a specific stream"""
    
    def __init__(self, stream_id: str, source_module_id: str):
        self.stream_id = stream_id
        self.source_module_id = source_module_id
        self.data_buffer = []
        self.subscribers = set()
        self.is_active = True
        self.created_at = datetime.utcnow()
        
    async def add_data(self, data: Any):
        """Add data to the stream"""
        if self.is_active:
            self.data_buffer.append(data)
            
            # Notify subscribers
            for subscriber in self.subscribers:
                # TODO: Send data to subscribers
                pass
                
    async def finalize(self):
        """Finalize the stream"""
        self.is_active = False
        # TODO: Notify subscribers that stream has ended
        
    def subscribe(self, module_id: str):
        """Subscribe to the stream"""
        self.subscribers.add(module_id)
        
    def unsubscribe(self, module_id: str):
        """Unsubscribe from the stream"""
        self.subscribers.discard(module_id)


# Main entry point
async def main():
    """Main function to start the MCP Hub"""
    config = {
        'websocket_host': '0.0.0.0',
        'websocket_port': 8765,
        'enable_websocket': True,
        'enable_grpc': True,
        'heartbeat_interval': 30,
        'heartbeat_timeout': 60,
        'enable_compression': True
    }
    
    hub = MCPHub(config)
    await hub.start()
    
    # Keep the server running
    try:
        await asyncio.Future()  # Run forever
    except KeyboardInterrupt:
        logger.info("Shutting down MCP Hub...")


if __name__ == "__main__":
    asyncio.run(main())

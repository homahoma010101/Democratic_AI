"""
API Gateway for Democratic AI Platform
Handles routing, rate limiting, authentication, and request/response transformation
"""

import asyncio
import json
import time
import uuid
from datetime import datetime
from typing import Dict, Any, Optional, List, Callable
from functools import wraps
import logging

from fastapi import FastAPI, Request, Response, HTTPException, Depends, Header, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
import websockets
import httpx

# Import core services
from core.auth.auth_service import AuthService, TokenPayload
from core.billing.billing_service import BillingService, MetricType
from core.mcp.hub_server import MCPHub

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Metrics
request_counter = Counter('api_requests_total', 'Total API requests', ['method', 'endpoint', 'status'])
request_duration = Histogram('api_request_duration_seconds', 'API request duration', ['method', 'endpoint'])
auth_failures = Counter('auth_failures_total', 'Total authentication failures', ['reason'])
rate_limit_hits = Counter('rate_limit_hits_total', 'Total rate limit hits', ['tenant'])


# Request/Response Models
class ModuleExecuteRequest(BaseModel):
    """Request model for module execution"""
    module_id: Optional[str] = None
    action: str
    payload: Dict[str, Any]
    timeout: Optional[int] = Field(default=30, ge=1, le=300)
    stream: Optional[bool] = False


class ModuleExecuteResponse(BaseModel):
    """Response model for module execution"""
    request_id: str
    status: str
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    execution_time: float
    credits_used: Optional[float] = None


class TenantContext(BaseModel):
    """Tenant context for requests"""
    tenant_id: str
    user_id: Optional[str] = None
    api_key_id: Optional[str] = None
    auth_type: str
    permissions: List[str]
    rate_limit: int


class APIGateway:
    """Main API Gateway implementation"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.app = FastAPI(
            title="Democratic AI Platform",
            description="Unified AI Module Marketplace and Execution Platform",
            version="2.0.0"
        )
        
        # Initialize services
        self.auth_service: Optional[AuthService] = None
        self.billing_service: Optional[BillingService] = None
        self.mcp_hub: Optional[MCPHub] = None
        
        # Rate limiter
        self.limiter = Limiter(key_func=self._get_rate_limit_key)
        
        # Circuit breaker state
        self.circuit_breakers: Dict[str, Dict] = {}
        
        # Request cache
        self.request_cache: Dict[str, Any] = {}
        self.cache_ttl = config.get('cache_ttl', 60)
        
        # Setup middleware and routes
        self._setup_middleware()
        self._setup_routes()
        self._setup_error_handlers()
        
    async def initialize(self):
        """Initialize the API Gateway"""
        # Initialize services
        self.auth_service = AuthService(self.config.get('auth', {}))
        await self.auth_service.initialize()
        
        self.billing_service = BillingService(self.config.get('billing', {}))
        await self.billing_service.initialize()
        
        self.mcp_hub = MCPHub(self.config.get('mcp', {}))
        await self.mcp_hub.start()
        
        logger.info("API Gateway initialized successfully")
        
    def _setup_middleware(self):
        """Setup FastAPI middleware"""
        # CORS
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=self.config.get('cors_origins', ["*"]),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset"]
        )
        
        # Gzip compression
        self.app.add_middleware(GZipMiddleware, minimum_size=1000)
        
        # Trusted host
        if self.config.get('trusted_hosts'):
            self.app.add_middleware(
                TrustedHostMiddleware,
                allowed_hosts=self.config.get('trusted_hosts')
            )
            
        # Custom middleware
        @self.app.middleware("http")
        async def add_process_time_header(request: Request, call_next):
            start_time = time.time()
            response = await call_next(request)
            process_time = time.time() - start_time
            response.headers["X-Process-Time"] = str(process_time)
            return response
            
        @self.app.middleware("http")
        async def log_requests(request: Request, call_next):
            request_id = str(uuid.uuid4())
            request.state.request_id = request_id
            
            logger.info(f"Request {request_id}: {request.method} {request.url.path}")
            
            response = await call_next(request)
            
            logger.info(f"Response {request_id}: {response.status_code}")
            request_counter.labels(
                method=request.method,
                endpoint=request.url.path,
                status=response.status_code
            ).inc()
            
            return response
            
    def _setup_routes(self):
        """Setup API routes"""
        # Health check
        @self.app.get("/health")
        async def health_check():
            return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}
            
        # Metrics endpoint
        @self.app.get("/metrics")
        async def metrics():
            return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
            
        # Module execution
        @self.app.post("/api/v1/execute", response_model=ModuleExecuteResponse)
        @self.limiter.limit("100/minute")
        async def execute_module(
            request: Request,
            execute_request: ModuleExecuteRequest,
            tenant_context: TenantContext = Depends(self.get_tenant_context)
        ):
            return await self._execute_module(execute_request, tenant_context)
            
        # Module discovery
        @self.app.get("/api/v1/modules")
        @self.limiter.limit("50/minute")
        async def list_modules(
            request: Request,
            capability: Optional[str] = None,
            tenant_context: TenantContext = Depends(self.get_tenant_context)
        ):
            return await self._list_modules(capability, tenant_context)
            
        # Module details
        @self.app.get("/api/v1/modules/{module_id}")
        @self.limiter.limit("100/minute")
        async def get_module(
            request: Request,
            module_id: str,
            tenant_context: TenantContext = Depends(self.get_tenant_context)
        ):
            return await self._get_module(module_id, tenant_context)
            
        # Usage statistics
        @self.app.get("/api/v1/usage")
        @self.limiter.limit("20/minute")
        async def get_usage(
            request: Request,
            start_date: Optional[datetime] = None,
            end_date: Optional[datetime] = None,
            tenant_context: TenantContext = Depends(self.get_tenant_context)
        ):
            return await self._get_usage(tenant_context, start_date, end_date)
            
        # Billing information
        @self.app.get("/api/v1/billing")
        @self.limiter.limit("20/minute")
        async def get_billing(
            request: Request,
            tenant_context: TenantContext = Depends(self.get_tenant_context)
        ):
            return await self._get_billing(tenant_context)
            
        # WebSocket endpoint for streaming
        @self.app.websocket("/ws")
        async def websocket_endpoint(websocket):
            await self._handle_websocket(websocket)
            
    def _setup_error_handlers(self):
        """Setup error handlers"""
        @self.app.exception_handler(RateLimitExceeded)
        async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
            response = JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"error": "Rate limit exceeded", "message": str(exc)}
            )
            response.headers["Retry-After"] = str(exc.retry_after)
            return response
            
        @self.app.exception_handler(HTTPException)
        async def http_exception_handler(request: Request, exc: HTTPException):
            return JSONResponse(
                status_code=exc.status_code,
                content={"error": exc.detail}
            )
            
        @self.app.exception_handler(Exception)
        async def general_exception_handler(request: Request, exc: Exception):
            logger.error(f"Unhandled exception: {exc}", exc_info=True)
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"error": "Internal server error"}
            )
            
    def _get_rate_limit_key(self, request: Request) -> str:
        """Get rate limit key from request"""
        # Try to get tenant ID from context
        if hasattr(request.state, 'tenant_id'):
            return f"tenant:{request.state.tenant_id}"
        # Fall back to IP address
        return get_remote_address(request)
        
    async def get_tenant_context(
        self,
        request: Request,
        authorization: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False)),
        x_api_key: Optional[str] = Header(None)
    ) -> TenantContext:
        """Get tenant context from request"""
        # Try JWT authentication
        if authorization and authorization.credentials:
            try:
                token_payload = await self.auth_service.verify_token(authorization.credentials)
                
                # Check if token is blacklisted
                if await self._is_token_blacklisted(token_payload.jti):
                    auth_failures.labels(reason='blacklisted_token').inc()
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Token has been revoked"
                    )
                    
                request.state.tenant_id = token_payload.tenant_id
                
                return TenantContext(
                    tenant_id=token_payload.tenant_id,
                    user_id=token_payload.sub,
                    auth_type="jwt",
                    permissions=token_payload.permissions,
                    rate_limit=10000  # Higher limit for authenticated users
                )
            except Exception as e:
                logger.debug(f"JWT validation failed: {e}")
                auth_failures.labels(reason='invalid_jwt').inc()
                
        # Try API key authentication
        if x_api_key:
            api_key = await self.auth_service.validate_api_key(x_api_key)
            if api_key:
                request.state.tenant_id = api_key.tenant_id
                
                return TenantContext(
                    tenant_id=api_key.tenant_id,
                    user_id=api_key.user_id,
                    api_key_id=api_key.key_id,
                    auth_type="api_key",
                    permissions=api_key.permissions,
                    rate_limit=api_key.rate_limit
                )
                
        # No valid authentication
        auth_failures.labels(reason='no_auth').inc()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
        
    async def _is_token_blacklisted(self, jti: str) -> bool:
        """Check if token is blacklisted"""
        # Check in Redis
        if self.auth_service.redis_client:
            blacklisted = await self.auth_service.redis_client.get(f"blacklist:{jti}")
            return blacklisted is not None
        return False
        
    async def _execute_module(
        self,
        execute_request: ModuleExecuteRequest,
        tenant_context: TenantContext
    ) -> ModuleExecuteResponse:
        """Execute a module through MCP Hub"""
        request_id = str(uuid.uuid4())
        start_time = time.time()
        
        try:
            # Check rate limit
            allowed, remaining = await self.billing_service.check_rate_limit(
                tenant_context.tenant_id,
                MetricType.API_CALLS
            )
            
            if not allowed:
                rate_limit_hits.labels(tenant=tenant_context.tenant_id).inc()
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Rate limit exceeded"
                )
                
            # Check circuit breaker
            if self._is_circuit_open(execute_request.module_id or execute_request.action):
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Service temporarily unavailable"
                )
                
            # Check cache
            cache_key = self._get_cache_key(execute_request, tenant_context)
            cached_result = self._get_cached_result(cache_key)
            if cached_result:
                return cached_result
                
            # Create MCP message
            message = {
                'id': request_id,
                'type': 'REQUEST',
                'source_module_id': 'api_gateway',
                'target_module_id': execute_request.module_id,
                'action': execute_request.action,
                'context': {
                    'tenant_id': tenant_context.tenant_id,
                    'user_id': tenant_context.user_id,
                    'session_id': request_id,
                    'timestamp': datetime.utcnow().isoformat()
                },
                'payload': execute_request.payload,
                'correlation_id': request_id,
                'priority': 5,
                'ttl': execute_request.timeout
            }
            
            # Send to MCP Hub
            if execute_request.stream:
                # Handle streaming response
                return await self._handle_streaming_execution(message, request_id)
            else:
                # Handle regular response
                response = await self.mcp_hub.route(message)
                
                execution_time = time.time() - start_time
                
                # Track usage
                await self.billing_service.track_usage(
                    tenant_context.tenant_id,
                    MetricType.API_CALLS,
                    1,
                    user_id=tenant_context.user_id,
                    module_id=execute_request.module_id,
                    metadata={
                        'action': execute_request.action,
                        'execution_time': execution_time
                    }
                )
                
                # Calculate credits used
                credits_used = self._calculate_credits(execution_time, len(json.dumps(response)))
                
                result = ModuleExecuteResponse(
                    request_id=request_id,
                    status="success",
                    result=response.get('payload'),
                    execution_time=execution_time,
                    credits_used=credits_used
                )
                
                # Cache result
                self._cache_result(cache_key, result)
                
                # Update circuit breaker
                self._record_success(execute_request.module_id or execute_request.action)
                
                return result
                
        except Exception as e:
            execution_time = time.time() - start_time
            
            # Record failure
            self._record_failure(execute_request.module_id or execute_request.action)
            
            logger.error(f"Module execution failed: {e}")
            
            return ModuleExecuteResponse(
                request_id=request_id,
                status="error",
                error=str(e),
                execution_time=execution_time
            )
            
    async def _handle_streaming_execution(
        self,
        message: Dict[str, Any],
        request_id: str
    ) -> StreamingResponse:
        """Handle streaming module execution"""
        async def stream_generator():
            try:
                # Connect to MCP Hub via WebSocket
                async with websockets.connect(f"ws://{self.config['mcp']['websocket_url']}") as ws:
                    # Send message
                    await ws.send(json.dumps(message))
                    
                    # Stream responses
                    while True:
                        response = await ws.recv()
                        data = json.loads(response)
                        
                        if data.get('type') == 'STREAM_END':
                            break
                            
                        yield f"data: {json.dumps(data)}\n\n"
                        
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                
        return StreamingResponse(
            stream_generator(),
            media_type="text/event-stream"
        )
        
    async def _list_modules(
        self,
        capability: Optional[str],
        tenant_context: TenantContext
    ) -> List[Dict[str, Any]]:
        """List available modules"""
        # Query MCP Hub for available modules
        message = {
            'id': str(uuid.uuid4()),
            'type': 'CAPABILITY_QUERY',
            'action': capability if capability else '',
            'context': {
                'tenant_id': tenant_context.tenant_id
            }
        }
        
        response = await self.mcp_hub.route(message)
        return response.get('modules', [])
        
    async def _get_module(
        self,
        module_id: str,
        tenant_context: TenantContext
    ) -> Dict[str, Any]:
        """Get module details"""
        # Query module registry
        # This would typically query a database or service
        return {
            'module_id': module_id,
            'name': 'Sample Module',
            'capabilities': ['text.analyze', 'text.summarize'],
            'version': '1.0.0',
            'description': 'A sample AI module'
        }
        
    async def _get_usage(
        self,
        tenant_context: TenantContext,
        start_date: Optional[datetime],
        end_date: Optional[datetime]
    ) -> Dict[str, Any]:
        """Get usage statistics"""
        if not start_date:
            start_date = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if not end_date:
            end_date = datetime.utcnow()
            
        usage_summary = await self.billing_service.get_usage_summary(
            tenant_context.tenant_id,
            start_date,
            end_date
        )
        
        return {
            'tenant_id': tenant_context.tenant_id,
            'period': {
                'start': start_date.isoformat(),
                'end': end_date.isoformat()
            },
            'usage': {
                'api_calls': usage_summary.api_calls,
                'compute_hours': float(usage_summary.compute_hours),
                'storage_gb': float(usage_summary.storage_gb),
                'bandwidth_gb': float(usage_summary.bandwidth_gb),
                'tokens_used': usage_summary.tokens_used,
                'credits_used': float(usage_summary.credits_used),
                'total_cost': float(usage_summary.total_cost)
            }
        }
        
    async def _get_billing(
        self,
        tenant_context: TenantContext
    ) -> Dict[str, Any]:
        """Get billing information"""
        subscription = await self.billing_service.get_active_subscription(tenant_context.tenant_id)
        
        if not subscription:
            return {'error': 'No active subscription'}
            
        return {
            'tenant_id': tenant_context.tenant_id,
            'subscription': {
                'plan': subscription.plan_type.value,
                'billing_period': subscription.billing_period.value,
                'start_date': subscription.start_date.isoformat(),
                'auto_renew': subscription.auto_renew
            }
        }
        
    async def _handle_websocket(self, websocket):
        """Handle WebSocket connections"""
        await websocket.accept()
        
        try:
            # Authentication
            auth_message = await websocket.receive_text()
            auth_data = json.loads(auth_message)
            
            # Validate token
            token = auth_data.get('token')
            if not token:
                await websocket.send_text(json.dumps({'error': 'Authentication required'}))
                await websocket.close()
                return
                
            token_payload = await self.auth_service.verify_token(token)
            
            # Connect to MCP Hub
            mcp_ws = await websockets.connect(f"ws://{self.config['mcp']['websocket_url']}")
            
            # Relay messages
            async def relay_to_mcp():
                async for message in websocket.iter_text():
                    await mcp_ws.send(message)
                    
            async def relay_from_mcp():
                async for message in mcp_ws:
                    await websocket.send_text(message)
                    
            await asyncio.gather(relay_to_mcp(), relay_from_mcp())
            
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
        finally:
            await websocket.close()
            
    def _get_cache_key(
        self,
        execute_request: ModuleExecuteRequest,
        tenant_context: TenantContext
    ) -> str:
        """Generate cache key for request"""
        key_parts = [
            tenant_context.tenant_id,
            execute_request.module_id or '',
            execute_request.action,
            json.dumps(execute_request.payload, sort_keys=True)
        ]
        return ':'.join(key_parts)
        
    def _get_cached_result(self, cache_key: str) -> Optional[ModuleExecuteResponse]:
        """Get cached result"""
        if cache_key in self.request_cache:
            cached = self.request_cache[cache_key]
            if time.time() - cached['timestamp'] < self.cache_ttl:
                return cached['result']
        return None
        
    def _cache_result(self, cache_key: str, result: ModuleExecuteResponse):
        """Cache result"""
        self.request_cache[cache_key] = {
            'result': result,
            'timestamp': time.time()
        }
        
        # Clean old cache entries
        if len(self.request_cache) > 1000:
            # Remove oldest entries
            sorted_keys = sorted(
                self.request_cache.keys(),
                key=lambda k: self.request_cache[k]['timestamp']
            )
            for key in sorted_keys[:100]:
                del self.request_cache[key]
                
    def _calculate_credits(self, execution_time: float, response_size: int) -> float:
        """Calculate credits used for request"""
        # Simple credit calculation
        # 1 credit per second of execution + 0.001 credit per KB of response
        time_credits = execution_time
        size_credits = response_size / 1024 * 0.001
        return time_credits + size_credits
        
    def _is_circuit_open(self, service_id: str) -> bool:
        """Check if circuit breaker is open"""
        if service_id not in self.circuit_breakers:
            self.circuit_breakers[service_id] = {
                'failures': 0,
                'successes': 0,
                'state': 'closed',
                'last_failure': None
            }
            
        breaker = self.circuit_breakers[service_id]
        
        if breaker['state'] == 'open':
            # Check if we should try half-open
            if breaker['last_failure']:
                time_since_failure = time.time() - breaker['last_failure']
                if time_since_failure > 60:  # 1 minute timeout
                    breaker['state'] = 'half-open'
                    return False
            return True
            
        return False
        
    def _record_success(self, service_id: str):
        """Record successful request"""
        if service_id in self.circuit_breakers:
            breaker = self.circuit_breakers[service_id]
            breaker['successes'] += 1
            
            if breaker['state'] == 'half-open' and breaker['successes'] > 5:
                breaker['state'] = 'closed'
                breaker['failures'] = 0
                
    def _record_failure(self, service_id: str):
        """Record failed request"""
        if service_id not in self.circuit_breakers:
            self.circuit_breakers[service_id] = {
                'failures': 0,
                'successes': 0,
                'state': 'closed',
                'last_failure': None
            }
            
        breaker = self.circuit_breakers[service_id]
        breaker['failures'] += 1
        breaker['last_failure'] = time.time()
        
        if breaker['failures'] > 5:
            breaker['state'] = 'open'


# Application factory
def create_app(config: Optional[Dict[str, Any]] = None) -> FastAPI:
    """Create FastAPI application"""
    if not config:
        config = {
            'auth': {
                'jwt_secret': 'your-secret-key-here',
                'db_host': 'localhost',
                'db_port': 5432,
                'redis_url': 'redis://localhost:6379'
            },
            'billing': {
                'stripe_api_key': 'sk_test_...',
                'db_host': 'localhost',
                'db_port': 5435,
                'redis_url': 'redis://localhost:6379/1'
            },
            'mcp': {
                'websocket_url': 'localhost:8765',
                'websocket_host': '0.0.0.0',
                'websocket_port': 8765
            },
            'cors_origins': ["*"],
            'cache_ttl': 60
        }
        
    gateway = APIGateway(config)
    
    @gateway.app.on_event("startup")
    async def startup_event():
        await gateway.initialize()
        
    @gateway.app.on_event("shutdown")
    async def shutdown_event():
        if gateway.auth_service:
            await gateway.auth_service.cleanup()
        if gateway.billing_service:
            await gateway.billing_service.cleanup()
            
    return gateway.app


# Main entry point
if __name__ == "__main__":
    import uvicorn
    
    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=8000)

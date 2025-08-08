"""
Authentication and Authorization Service for Democratic AI
Handles multi-tenant authentication, JWT tokens, API keys, and RBAC
"""

import asyncio
import secrets
import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List, Set
from dataclasses import dataclass, field
from enum import Enum
import jwt
import bcrypt
import asyncpg
import redis.asyncio as redis
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class UserRole(Enum):
    """User roles in the system"""
    SUPER_ADMIN = "super_admin"
    TENANT_ADMIN = "tenant_admin"
    DEVELOPER = "developer"
    USER = "user"
    VIEWER = "viewer"


class AuthType(Enum):
    """Authentication types"""
    JWT = "jwt"
    API_KEY = "api_key"
    OAUTH2 = "oauth2"
    SAML = "saml"


@dataclass
class User:
    """User entity"""
    user_id: str
    tenant_id: str
    email: str
    username: str
    full_name: str
    role: UserRole
    is_active: bool = True
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    last_login: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Tenant:
    """Tenant entity"""
    tenant_id: str
    name: str
    slug: str
    plan_type: str
    is_active: bool = True
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    settings: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class APIKey:
    """API Key entity"""
    key_id: str
    tenant_id: str
    user_id: str
    name: str
    key_hash: str
    permissions: List[str]
    rate_limit: int
    is_active: bool = True
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_used: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Session:
    """User session"""
    session_id: str
    user_id: str
    tenant_id: str
    token: str
    refresh_token: str
    ip_address: str
    user_agent: str
    created_at: datetime = field(default_factory=datetime.utcnow)
    expires_at: datetime = field(default_factory=lambda: datetime.utcnow() + timedelta(hours=24))
    is_active: bool = True


@dataclass
class TokenPayload:
    """JWT token payload"""
    sub: str  # Subject (user_id)
    tenant_id: str
    email: str
    role: str
    permissions: List[str]
    jti: str  # JWT ID
    iat: datetime  # Issued at
    exp: datetime  # Expiration


@dataclass
class LoginRequest:
    """Login request data"""
    email: str
    password: str
    tenant_slug: str
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None


@dataclass
class TokenResponse:
    """Token response data"""
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int = 3600
    refresh_expires_in: int = 86400


class AuthService:
    """Main authentication service"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        
        # JWT configuration
        self.jwt_secret = config.get('jwt_secret', secrets.token_urlsafe(32))
        self.jwt_algorithm = config.get('jwt_algorithm', 'HS256')
        self.jwt_expiry = config.get('jwt_expiry', 3600)  # 1 hour
        self.refresh_expiry = config.get('refresh_expiry', 86400 * 7)  # 7 days
        
        # Database connections
        self.db_pool: Optional[asyncpg.Pool] = None
        self.redis_client: Optional[redis.Redis] = None
        
        # Permission definitions (Role-Based Access Control)
        self.permissions = {
            UserRole.SUPER_ADMIN: {"*"},  # All permissions
            UserRole.TENANT_ADMIN: {
                "tenant:read", "tenant:update",
                "user:create", "user:read", "user:update", "user:delete",
                "module:*", "api_key:*", "billing:read"
            },
            UserRole.DEVELOPER: {
                "tenant:read",
                "user:read:self", "user:update:self",
                "module:create", "module:read", "module:update", "module:delete:own",
                "api_key:create:own", "api_key:read:own", "api_key:delete:own"
            },
            UserRole.USER: {
                "tenant:read",
                "user:read:self", "user:update:self",
                "module:read", "module:use",
                "api_key:read:own"
            },
            UserRole.VIEWER: {
                "tenant:read",
                "user:read:self",
                "module:read"
            }
        }
        
        # Session cache
        self.sessions_cache: Dict[str, Session] = {}
        
        # Blacklisted tokens
        self.token_blacklist: Set[str] = set()
        
    async def initialize(self):
        """Initialize the auth service"""
        # Connect to PostgreSQL
        self.db_pool = await asyncpg.create_pool(
            host=self.config.get('db_host', 'localhost'),
            port=self.config.get('db_port', 5432),
            database=self.config.get('db_name', 'democratic_ai_auth'),
            user=self.config.get('db_user', 'postgres'),
            password=self.config.get('db_password', 'password'),
            min_size=10,
            max_size=20
        )
        
        # Connect to Redis
        self.redis_client = await redis.from_url(
            self.config.get('redis_url', 'redis://localhost:6379'),
            encoding='utf-8',
            decode_responses=True
        )
        
        # Create database schema
        await self._create_schema()
        
        logger.info("Auth service initialized successfully")
        
    async def _create_schema(self):
        """Create database schema"""
        async with self.db_pool.acquire() as conn:
            # Create tenants table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS tenants (
                    tenant_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    name VARCHAR(255) NOT NULL,
                    slug VARCHAR(255) UNIQUE NOT NULL,
                    plan_type VARCHAR(50) NOT NULL DEFAULT 'starter',
                    is_active BOOLEAN DEFAULT true,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    settings JSONB DEFAULT '{}',
                    metadata JSONB DEFAULT '{}'
                )
            """)
            
            # Create users table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                    email VARCHAR(255) NOT NULL,
                    username VARCHAR(255) NOT NULL,
                    password_hash VARCHAR(255) NOT NULL,
                    full_name VARCHAR(255),
                    role VARCHAR(50) NOT NULL DEFAULT 'user',
                    is_active BOOLEAN DEFAULT true,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_login TIMESTAMP,
                    metadata JSONB DEFAULT '{}',
                    UNIQUE(tenant_id, email),
                    UNIQUE(tenant_id, username)
                )
            """)
            
            # Create API keys table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS api_keys (
                    key_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                    user_id UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    name VARCHAR(255) NOT NULL,
                    key_hash VARCHAR(255) NOT NULL UNIQUE,
                    permissions TEXT[],
                    rate_limit INTEGER DEFAULT 1000,
                    is_active BOOLEAN DEFAULT true,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used TIMESTAMP,
                    expires_at TIMESTAMP,
                    metadata JSONB DEFAULT '{}'
                )
            """)
            
            # Create sessions table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                    token_hash VARCHAR(255) NOT NULL,
                    refresh_token_hash VARCHAR(255) NOT NULL,
                    ip_address INET,
                    user_agent TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL,
                    is_active BOOLEAN DEFAULT true
                )
            """)
            
            # Create audit log table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS auth_audit_log (
                    log_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    tenant_id UUID REFERENCES tenants(tenant_id) ON DELETE SET NULL,
                    user_id UUID REFERENCES users(user_id) ON DELETE SET NULL,
                    action VARCHAR(100) NOT NULL,
                    resource_type VARCHAR(50),
                    resource_id VARCHAR(255),
                    ip_address INET,
                    user_agent TEXT,
                    success BOOLEAN NOT NULL,
                    error_message TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Create indexes
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_tenant ON users(tenant_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_tenant ON auth_audit_log(tenant_id)")
            
    async def create_tenant(self, name: str, slug: str, plan_type: str = "starter") -> Tenant:
        """Create a new tenant"""
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO tenants (name, slug, plan_type)
                VALUES ($1, $2, $3)
                RETURNING *
            """, name, slug, plan_type)
            
            tenant = Tenant(
                tenant_id=str(row['tenant_id']),
                name=row['name'],
                slug=row['slug'],
                plan_type=row['plan_type'],
                is_active=row['is_active'],
                created_at=row['created_at'],
                updated_at=row['updated_at'],
                settings=row['settings'],
                metadata=row['metadata']
            )
            
            await self.audit_log(
                tenant_id=tenant.tenant_id,
                action="tenant.create",
                resource_type="tenant",
                resource_id=tenant.tenant_id,
                success=True
            )
            
            return tenant
            
    async def create_user(self, tenant_id: str, email: str, username: str, 
                         password: str, full_name: str, role: UserRole = UserRole.USER) -> User:
        """Create a new user"""
        # Hash password
        password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        
        async with self.db_pool.acquire() as conn:
            try:
                row = await conn.fetchrow("""
                    INSERT INTO users (tenant_id, email, username, password_hash, full_name, role)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    RETURNING *
                """, uuid.UUID(tenant_id), email, username, password_hash, full_name, role.value)
                
                user = User(
                    user_id=str(row['user_id']),
                    tenant_id=str(row['tenant_id']),
                    email=row['email'],
                    username=row['username'],
                    full_name=row['full_name'],
                    role=UserRole(row['role']),
                    is_active=row['is_active'],
                    created_at=row['created_at'],
                    updated_at=row['updated_at'],
                    last_login=row['last_login'],
                    metadata=row['metadata']
                )
                
                await self.audit_log(
                    tenant_id=tenant_id,
                    user_id=user.user_id,
                    action="user.create",
                    resource_type="user",
                    resource_id=user.user_id,
                    success=True
                )
                
                return user
                
            except asyncpg.UniqueViolationError as e:
                await self.audit_log(
                    tenant_id=tenant_id,
                    action="user.create",
                    resource_type="user",
                    success=False,
                    error_message=f"User already exists: {email}"
                )
                raise ValueError(f"User already exists: {email}")
                
    async def authenticate(self, login_request: LoginRequest) -> TokenResponse:
        """Authenticate a user and return tokens"""
        async with self.db_pool.acquire() as conn:
            # Get user with tenant info
            row = await conn.fetchrow("""
                SELECT u.*, t.slug as tenant_slug
                FROM users u
                JOIN tenants t ON u.tenant_id = t.tenant_id
                WHERE u.email = $1 AND t.slug = $2 AND u.is_active = true
            """, login_request.email, login_request.tenant_slug)
            
            if not row:
                await self.audit_log(
                    action="user.login",
                    resource_type="user",
                    success=False,
                    error_message=f"Invalid credentials for {login_request.email}",
                    ip_address=login_request.ip_address
                )
                raise ValueError("Invalid credentials")
                
            # Verify password
            if not bcrypt.checkpw(login_request.password.encode('utf-8'), 
                                row['password_hash'].encode('utf-8')):
                await self.audit_log(
                    tenant_id=str(row['tenant_id']),
                    user_id=str(row['user_id']),
                    action="user.login",
                    resource_type="user",
                    resource_id=str(row['user_id']),
                    success=False,
                    error_message="Invalid password",
                    ip_address=login_request.ip_address
                )
                raise ValueError("Invalid credentials")
                
            # Generate tokens
            access_token = self._generate_access_token(
                user_id=str(row['user_id']),
                tenant_id=str(row['tenant_id']),
                email=row['email'],
                role=UserRole(row['role'])
            )
            
            refresh_token = await self._generate_refresh_token(user_id=str(row['user_id']))
            
            # Create session
            session = await self._create_session(
                user_id=str(row['user_id']),
                tenant_id=str(row['tenant_id']),
                access_token=access_token,
                refresh_token=refresh_token,
                ip_address=login_request.ip_address,
                user_agent=login_request.user_agent
            )
            
            # Update last login
            await conn.execute("""
                UPDATE users SET last_login = $1 WHERE user_id = $2
            """, datetime.utcnow(), row['user_id'])
            
            await self.audit_log(
                tenant_id=str(row['tenant_id']),
                user_id=str(row['user_id']),
                action="user.login",
                resource_type="user",
                resource_id=str(row['user_id']),
                success=True,
                ip_address=login_request.ip_address
            )
            
            return TokenResponse(
                access_token=access_token,
                refresh_token=refresh_token,
                expires_in=self.jwt_expiry,
                refresh_expires_in=self.refresh_expiry
            )
            
    def _generate_access_token(self, user_id: str, tenant_id: str, email: str, role: UserRole) -> str:
        """Generate JWT access token"""
        now = datetime.utcnow()
        exp = now + timedelta(seconds=self.jwt_expiry)
        
        # Get permissions for role
        role_permissions = list(self.permissions.get(role, set()))
        
        payload = {
            'sub': user_id,
            'tenant_id': tenant_id,
            'email': email,
            'role': role.value,
            'permissions': role_permissions,
            'jti': str(uuid.uuid4()),
            'iat': now,
            'exp': exp
        }
        
        return jwt.encode(payload, self.jwt_secret, algorithm=self.jwt_algorithm)
        
    async def _generate_refresh_token(self, user_id: str) -> str:
        """Generate refresh token"""
        token = secrets.token_urlsafe(32)
        
        # Store in Redis with expiry
        await self.redis_client.setex(
            f"refresh_token:{token}",
            self.refresh_expiry,
            user_id
        )
        
        return token
        
    async def _create_session(self, user_id: str, tenant_id: str, access_token: str, 
                            refresh_token: str, ip_address: Optional[str], 
                            user_agent: Optional[str]) -> Session:
        """Create a new session"""
        token_hash = hashlib.sha256(access_token.encode()).hexdigest()
        refresh_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
        
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO sessions (user_id, tenant_id, token_hash, refresh_token_hash, 
                                    ip_address, user_agent, expires_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING *
            """, uuid.UUID(user_id), uuid.UUID(tenant_id), token_hash, refresh_hash,
                ip_address, user_agent, datetime.utcnow() + timedelta(seconds=self.jwt_expiry))
            
            session = Session(
                session_id=str(row['session_id']),
                user_id=user_id,
                tenant_id=tenant_id,
                token=access_token,
                refresh_token=refresh_token,
                ip_address=ip_address or '',
                user_agent=user_agent or '',
                created_at=row['created_at'],
                expires_at=row['expires_at'],
                is_active=row['is_active']
            )
            
            # Cache session
            self.sessions_cache[access_token] = session
            
            return session
            
    async def verify_token(self, token: str) -> TokenPayload:
        """Verify and decode JWT token"""
        try:
            # Check if token is blacklisted
            if token in self.token_blacklist:
                raise ValueError("Token has been revoked")
                
            # Decode token
            payload = jwt.decode(token, self.jwt_secret, algorithms=[self.jwt_algorithm])
            
            # Check if session exists in cache
            if token not in self.sessions_cache:
                # Check in database
                token_hash = hashlib.sha256(token.encode()).hexdigest()
                async with self.db_pool.acquire() as conn:
                    row = await conn.fetchrow("""
                        SELECT * FROM sessions 
                        WHERE token_hash = $1 AND is_active = true AND expires_at > $2
                    """, token_hash, datetime.utcnow())
                    
                    if not row:
                        raise ValueError("Invalid or expired session")
                        
            return TokenPayload(
                sub=payload['sub'],
                tenant_id=payload['tenant_id'],
                email=payload['email'],
                role=payload['role'],
                permissions=payload['permissions'],
                jti=payload['jti'],
                iat=datetime.fromtimestamp(payload['iat']),
                exp=datetime.fromtimestamp(payload['exp'])
            )
            
        except jwt.ExpiredSignatureError:
            raise ValueError("Token has expired")
        except jwt.InvalidTokenError as e:
            raise ValueError(f"Invalid token: {e}")
            
    async def refresh_access_token(self, refresh_token: str) -> TokenResponse:
        """Refresh access token using refresh token"""
        # Get user_id from Redis
        user_id = await self.redis_client.get(f"refresh_token:{refresh_token}")
        
        if not user_id:
            raise ValueError("Invalid or expired refresh token")
            
        # Get user info
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM users WHERE user_id = $1 AND is_active = true
            """, uuid.UUID(user_id))
            
            if not row:
                raise ValueError("User not found or inactive")
                
            # Generate new access token
            access_token = self._generate_access_token(
                user_id=str(row['user_id']),
                tenant_id=str(row['tenant_id']),
                email=row['email'],
                role=UserRole(row['role'])
            )
            
            # Generate new refresh token
            new_refresh_token = await self._generate_refresh_token(user_id)
            
            # Invalidate old refresh token
            await self.redis_client.delete(f"refresh_token:{refresh_token}")
            
            return TokenResponse(
                access_token=access_token,
                refresh_token=new_refresh_token,
                expires_in=self.jwt_expiry,
                refresh_expires_in=self.refresh_expiry
            )
            
    async def revoke_token(self, token: str):
        """Revoke an access token"""
        # Add to blacklist
        self.token_blacklist.add(token)
        
        # Store in Redis with expiry
        await self.redis_client.setex(
            f"blacklist:{token}",
            self.jwt_expiry,
            "1"
        )
        
        # Remove from session cache
        if token in self.sessions_cache:
            del self.sessions_cache[token]
            
        # Deactivate in database
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        async with self.db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE sessions SET is_active = false WHERE token_hash = $1
            """, token_hash)
            
    async def create_api_key(self, tenant_id: str, user_id: str, name: str,
                           permissions: List[str], rate_limit: int = 1000,
                           expires_at: Optional[datetime] = None) -> str:
        """Create a new API key"""
        # Generate API key
        api_key = f"dk_{secrets.token_urlsafe(32)}"
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO api_keys (tenant_id, user_id, name, key_hash, permissions, 
                                    rate_limit, expires_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING key_id
            """, uuid.UUID(tenant_id), uuid.UUID(user_id), name, key_hash, 
                permissions, rate_limit, expires_at)
            
            await self.audit_log(
                tenant_id=tenant_id,
                user_id=user_id,
                action="api_key.create",
                resource_type="api_key",
                resource_id=str(row['key_id']),
                success=True
            )
            
            return api_key
            
    async def validate_api_key(self, api_key: str) -> Optional[APIKey]:
        """Validate an API key"""
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        
        # Check cache first
        cached = await self.redis_client.get(f"api_key:{key_hash}")
        if cached:
            return APIKey(**json.loads(cached))
            
        # Check database
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM api_keys 
                WHERE key_hash = $1 AND is_active = true 
                AND (expires_at IS NULL OR expires_at > $2)
            """, key_hash, datetime.utcnow())
            
            if not row:
                return None
                
            # Update last used
            await conn.execute("""
                UPDATE api_keys SET last_used = $1 WHERE key_id = $2
            """, datetime.utcnow(), row['key_id'])
            
            api_key = APIKey(
                key_id=str(row['key_id']),
                tenant_id=str(row['tenant_id']),
                user_id=str(row['user_id']),
                name=row['name'],
                key_hash=row['key_hash'],
                permissions=row['permissions'],
                rate_limit=row['rate_limit'],
                is_active=row['is_active'],
                created_at=row['created_at'],
                last_used=row['last_used'],
                expires_at=row['expires_at'],
                metadata=row['metadata']
            )
            
            # Cache for 5 minutes
            await self.redis_client.setex(
                f"api_key:{key_hash}",
                300,
                json.dumps({
                    'key_id': api_key.key_id,
                    'tenant_id': api_key.tenant_id,
                    'user_id': api_key.user_id,
                    'name': api_key.name,
                    'key_hash': api_key.key_hash,
                    'permissions': api_key.permissions,
                    'rate_limit': api_key.rate_limit
                })
            )
            
            return api_key
            
    async def check_permission(self, user_id: str, permission: str) -> bool:
        """Check if a user has a specific permission"""
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT role FROM users WHERE user_id = $1 AND is_active = true
            """, uuid.UUID(user_id))
            
            if not row:
                return False
                
            role = UserRole(row['role'])
            role_permissions = self.permissions.get(role, set())
            
            # Check for wildcard permission
            if "*" in role_permissions:
                return True
                
            # Check for specific permission
            if permission in role_permissions:
                return True
                
            # Check for wildcard in permission category
            category = permission.split(':')[0]
            if f"{category}:*" in role_permissions:
                return True
                
            return False
            
    async def audit_log(self, action: str, resource_type: Optional[str] = None,
                       resource_id: Optional[str] = None, tenant_id: Optional[str] = None,
                       user_id: Optional[str] = None, ip_address: Optional[str] = None,
                       user_agent: Optional[str] = None, success: bool = True,
                       error_message: Optional[str] = None):
        """Log an audit event"""
        async with self.db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO auth_audit_log (tenant_id, user_id, action, resource_type, 
                                          resource_id, ip_address, user_agent, success, error_message)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """, uuid.UUID(tenant_id) if tenant_id else None,
                uuid.UUID(user_id) if user_id else None,
                action, resource_type, resource_id, ip_address, user_agent,
                success, error_message)
                
    async def get_user_by_id(self, user_id: str) -> Optional[User]:
        """Get user by ID"""
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM users WHERE user_id = $1
            """, uuid.UUID(user_id))
            
            if not row:
                return None
                
            return User(
                user_id=str(row['user_id']),
                tenant_id=str(row['tenant_id']),
                email=row['email'],
                username=row['username'],
                full_name=row['full_name'],
                role=UserRole(row['role']),
                is_active=row['is_active'],
                created_at=row['created_at'],
                updated_at=row['updated_at'],
                last_login=row['last_login'],
                metadata=row['metadata']
            )
            
    async def update_user(self, user_id: str, **kwargs) -> User:
        """Update user information"""
        allowed_fields = ['email', 'username', 'full_name', 'role', 'is_active', 'metadata']
        updates = {k: v for k, v in kwargs.items() if k in allowed_fields}
        
        if not updates:
            raise ValueError("No valid fields to update")
            
        # Build update query
        set_clauses = [f"{field} = ${i+2}" for i, field in enumerate(updates.keys())]
        query = f"""
            UPDATE users 
            SET {', '.join(set_clauses)}, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = $1
            RETURNING *
        """
        
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(query, uuid.UUID(user_id), *updates.values())
            
            if not row:
                raise ValueError(f"User {user_id} not found")
                
            return User(
                user_id=str(row['user_id']),
                tenant_id=str(row['tenant_id']),
                email=row['email'],
                username=row['username'],
                full_name=row['full_name'],
                role=UserRole(row['role']),
                is_active=row['is_active'],
                created_at=row['created_at'],
                updated_at=row['updated_at'],
                last_login=row['last_login'],
                metadata=row['metadata']
            )
            
    async def delete_user(self, user_id: str):
        """Delete a user (soft delete)"""
        async with self.db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE users SET is_active = false WHERE user_id = $1
            """, uuid.UUID(user_id))
            
            await self.audit_log(
                user_id=user_id,
                action="user.delete",
                resource_type="user",
                resource_id=user_id,
                success=True
            )
            
    async def cleanup(self):
        """Clean up resources"""
        if self.db_pool:
            await self.db_pool.close()
        if self.redis_client:
            await self.redis_client.close()


# Example usage
async def main():
    config = {
        'jwt_secret': 'your-secret-key-here',
        'db_host': 'localhost',
        'db_port': 5432,
        'db_name': 'democratic_ai_auth',
        'db_user': 'postgres',
        'db_password': 'password',
        'redis_url': 'redis://localhost:6379'
    }
    
    auth_service = AuthService(config)
    await auth_service.initialize()
    
    # Create a tenant
    tenant = await auth_service.create_tenant("Test Company", "test-company")
    
    # Create a user
    user = await auth_service.create_user(
        tenant_id=tenant.tenant_id,
        email="admin@test.com",
        username="admin",
        password="SecurePassword123!",
        full_name="Admin User",
        role=UserRole.TENANT_ADMIN
    )
    
    # Authenticate
    login_request = LoginRequest(
        email="admin@test.com",
        password="SecurePassword123!",
        tenant_slug="test-company"
    )
    
    token_response = await auth_service.authenticate(login_request)
    print(f"Access Token: {token_response.access_token}")
    
    # Verify token
    payload = await auth_service.verify_token(token_response.access_token)
    print(f"Token Payload: {payload}")
    
    await auth_service.cleanup()


if __name__ == "__main__":
    asyncio.run(main())

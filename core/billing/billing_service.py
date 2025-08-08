"""
Billing and Usage Tracking Service for Democratic AI
Handles usage-based billing, invoice generation, and payment processing
"""

import asyncio
import uuid
import json
from datetime import datetime, timedelta, date
from decimal import Decimal
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from enum import Enum
import asyncpg
import redis.asyncio as redis
import stripe
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class PlanType(Enum):
    """Subscription plan types"""
    FREE = "free"
    STARTER = "starter"
    PROFESSIONAL = "professional"
    ENTERPRISE = "enterprise"
    CUSTOM = "custom"


class BillingPeriod(Enum):
    """Billing period types"""
    MONTHLY = "monthly"
    YEARLY = "yearly"
    QUARTERLY = "quarterly"


class InvoiceStatus(Enum):
    """Invoice status"""
    DRAFT = "draft"
    PENDING = "pending"
    PAID = "paid"
    OVERDUE = "overdue"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class PaymentMethod(Enum):
    """Payment methods"""
    CREDIT_CARD = "credit_card"
    DEBIT_CARD = "debit_card"
    BANK_TRANSFER = "bank_transfer"
    PAYPAL = "paypal"
    CRYPTO = "crypto"


class MetricType(Enum):
    """Usage metric types"""
    API_CALLS = "api_calls"
    COMPUTE_TIME = "compute_time"
    STORAGE_BYTES = "storage_bytes"
    BANDWIDTH_BYTES = "bandwidth_bytes"
    TOKENS = "tokens"
    CREDITS = "credits"
    CUSTOM = "custom"


@dataclass
class PricingTier:
    """Pricing tier configuration"""
    tier_id: str
    name: str
    plan_type: PlanType
    base_price_monthly: Decimal
    base_price_yearly: Decimal
    included_usage: Dict[str, int]
    overage_rates: Dict[str, Decimal]
    features: List[str]
    limits: Dict[str, int]
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Subscription:
    """Customer subscription"""
    subscription_id: str
    tenant_id: str
    plan_type: PlanType
    billing_period: BillingPeriod
    start_date: datetime
    end_date: Optional[datetime]
    is_active: bool
    auto_renew: bool
    payment_method_id: Optional[str]
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class UsageEvent:
    """Usage tracking event"""
    event_id: str
    tenant_id: str
    user_id: Optional[str]
    metric_type: MetricType
    value: Decimal
    timestamp: datetime
    module_id: Optional[str]
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Invoice:
    """Invoice for billing"""
    invoice_id: str
    tenant_id: str
    invoice_number: str
    period_start: datetime
    period_end: datetime
    subtotal: Decimal
    tax: Decimal
    total: Decimal
    status: InvoiceStatus
    due_date: datetime
    paid_at: Optional[datetime]
    line_items: List[Dict[str, Any]]
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PaymentInfo:
    """Payment information"""
    payment_id: str
    tenant_id: str
    amount: Decimal
    currency: str
    payment_method: PaymentMethod
    status: str
    processed_at: datetime
    reference: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class UsageSummary:
    """Usage summary for a period"""
    tenant_id: str
    period_start: datetime
    period_end: datetime
    api_calls: int
    compute_hours: Decimal
    storage_gb: Decimal
    bandwidth_gb: Decimal
    tokens_used: int
    credits_used: Decimal
    total_cost: Decimal


class BillingService:
    """Main billing and usage tracking service"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        
        # Database connections
        self.db_pool: Optional[asyncpg.Pool] = None
        self.redis_client: Optional[redis.Redis] = None
        
        # Stripe configuration
        stripe.api_key = config.get('stripe_api_key')
        
        # Pricing tiers
        self.pricing_tiers: Dict[str, PricingTier] = {}
        
        # Usage buffers for batch processing
        self.usage_buffer: List[UsageEvent] = []
        self.buffer_size = config.get('buffer_size', 1000)
        self.flush_interval = config.get('flush_interval', 5)  # seconds
        
        # Rate limiting
        self.rate_limits: Dict[str, Dict[str, int]] = {}
        
    async def initialize(self):
        """Initialize the billing service"""
        # Connect to PostgreSQL
        self.db_pool = await asyncpg.create_pool(
            host=self.config.get('db_host', 'localhost'),
            port=self.config.get('db_port', 5435),  # Different port for billing DB
            database=self.config.get('db_name', 'democratic_ai_billing'),
            user=self.config.get('db_user', 'postgres'),
            password=self.config.get('db_password', 'password'),
            min_size=10,
            max_size=20
        )
        
        # Connect to Redis
        self.redis_client = await redis.from_url(
            self.config.get('redis_url', 'redis://localhost:6379/1'),
            encoding='utf-8',
            decode_responses=True
        )
        
        # Create database schema
        await self._create_schema()
        
        # Load pricing tiers
        self._load_pricing_tiers()
        
        # Start background tasks
        asyncio.create_task(self._usage_aggregator())
        asyncio.create_task(self._invoice_generator())
        asyncio.create_task(self._payment_processor())
        
        logger.info("Billing service initialized successfully")
        
    async def _create_schema(self):
        """Create database schema for billing"""
        async with self.db_pool.acquire() as conn:
            # Subscriptions table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    subscription_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    tenant_id UUID NOT NULL,
                    plan_type VARCHAR(50) NOT NULL,
                    billing_period VARCHAR(20) NOT NULL,
                    start_date TIMESTAMP NOT NULL,
                    end_date TIMESTAMP,
                    is_active BOOLEAN DEFAULT true,
                    auto_renew BOOLEAN DEFAULT true,
                    payment_method_id VARCHAR(255),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    metadata JSONB DEFAULT '{}'
                )
            """)
            
            # Usage events table (partitioned by month)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS usage_events (
                    event_id UUID DEFAULT gen_random_uuid(),
                    tenant_id UUID NOT NULL,
                    user_id UUID,
                    metric_type VARCHAR(50) NOT NULL,
                    value DECIMAL(20, 6) NOT NULL,
                    timestamp TIMESTAMP NOT NULL,
                    module_id VARCHAR(255),
                    metadata JSONB DEFAULT '{}',
                    PRIMARY KEY (event_id, timestamp)
                ) PARTITION BY RANGE (timestamp)
            """)
            
            # Create partitions for current and next month
            current_month = datetime.now().strftime('%Y_%m')
            next_month = (datetime.now() + timedelta(days=32)).strftime('%Y_%m')
            
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS usage_events_{current_month} 
                PARTITION OF usage_events
                FOR VALUES FROM ('{datetime.now().strftime('%Y-%m-01')}') 
                TO ('{(datetime.now() + timedelta(days=32)).strftime('%Y-%m-01')}')
            """)
            
            # Usage aggregates table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS usage_aggregates (
                    aggregate_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    tenant_id UUID NOT NULL,
                    date DATE NOT NULL,
                    metric_type VARCHAR(50) NOT NULL,
                    total_value DECIMAL(20, 6) NOT NULL,
                    count INTEGER NOT NULL,
                    metadata JSONB DEFAULT '{}',
                    UNIQUE(tenant_id, date, metric_type)
                )
            """)
            
            # Invoices table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS invoices (
                    invoice_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    tenant_id UUID NOT NULL,
                    invoice_number VARCHAR(50) UNIQUE NOT NULL,
                    period_start TIMESTAMP NOT NULL,
                    period_end TIMESTAMP NOT NULL,
                    subtotal DECIMAL(10, 2) NOT NULL,
                    tax DECIMAL(10, 2) DEFAULT 0,
                    total DECIMAL(10, 2) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'draft',
                    due_date TIMESTAMP NOT NULL,
                    paid_at TIMESTAMP,
                    line_items JSONB NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    metadata JSONB DEFAULT '{}'
                )
            """)
            
            # Payments table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    payment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    tenant_id UUID NOT NULL,
                    invoice_id UUID REFERENCES invoices(invoice_id),
                    amount DECIMAL(10, 2) NOT NULL,
                    currency VARCHAR(3) NOT NULL DEFAULT 'JPY',
                    payment_method VARCHAR(50) NOT NULL,
                    status VARCHAR(20) NOT NULL,
                    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    reference VARCHAR(255),
                    metadata JSONB DEFAULT '{}'
                )
            """)
            
            # Payment methods table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS payment_methods (
                    method_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    tenant_id UUID NOT NULL,
                    type VARCHAR(50) NOT NULL,
                    stripe_payment_method_id VARCHAR(255),
                    last4 VARCHAR(4),
                    brand VARCHAR(20),
                    is_default BOOLEAN DEFAULT false,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    metadata JSONB DEFAULT '{}'
                )
            """)
            
            # Create indexes
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_tenant ON usage_events(tenant_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_timestamp ON usage_events(timestamp)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_aggregates_tenant_date ON usage_aggregates(tenant_id, date)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_tenant ON invoices(tenant_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_tenant ON payments(tenant_id)")
            
    def _load_pricing_tiers(self):
        """Load pricing tier configurations"""
        self.pricing_tiers = {
            'free': PricingTier(
                tier_id='free',
                name='Free',
                plan_type=PlanType.FREE,
                base_price_monthly=Decimal('0'),
                base_price_yearly=Decimal('0'),
                included_usage={
                    MetricType.API_CALLS.value: 1000,
                    MetricType.COMPUTE_TIME.value: 1,  # hours
                    MetricType.STORAGE_BYTES.value: 1024**3,  # 1GB
                },
                overage_rates={},  # No overages on free plan
                features=['Basic API Access', 'Community Support'],
                limits={
                    'max_users': 1,
                    'max_modules': 3,
                    'max_api_keys': 1
                }
            ),
            'starter': PricingTier(
                tier_id='starter',
                name='Starter',
                plan_type=PlanType.STARTER,
                base_price_monthly=Decimal('4900'),  # ¥4,900/month
                base_price_yearly=Decimal('49000'),   # ¥49,000/year (2 months free)
                included_usage={
                    MetricType.API_CALLS.value: 100000,
                    MetricType.COMPUTE_TIME.value: 100,  # hours
                    MetricType.STORAGE_BYTES.value: 50 * 1024**3,  # 50GB
                },
                overage_rates={
                    MetricType.API_CALLS.value: Decimal('0.05'),  # ¥0.05/call
                    MetricType.COMPUTE_TIME.value: Decimal('50'),  # ¥50/hour
                    MetricType.STORAGE_BYTES.value: Decimal('10'),  # ¥10/GB
                },
                features=['Email Support', 'API Access', 'Custom Modules', 'Basic Analytics'],
                limits={
                    'max_users': 10,
                    'max_modules': 20,
                    'max_api_keys': 10
                }
            ),
            'professional': PricingTier(
                tier_id='professional',
                name='Professional',
                plan_type=PlanType.PROFESSIONAL,
                base_price_monthly=Decimal('19900'),  # ¥19,900/month
                base_price_yearly=Decimal('199000'),   # ¥199,000/year
                included_usage={
                    MetricType.API_CALLS.value: 1000000,
                    MetricType.COMPUTE_TIME.value: 500,  # hours
                    MetricType.STORAGE_BYTES.value: 500 * 1024**3,  # 500GB
                },
                overage_rates={
                    MetricType.API_CALLS.value: Decimal('0.03'),
                    MetricType.COMPUTE_TIME.value: Decimal('40'),
                    MetricType.STORAGE_BYTES.value: Decimal('8'),
                },
                features=['Priority Support', 'Advanced Analytics', 'Custom Integrations', 'SLA'],
                limits={
                    'max_users': 50,
                    'max_modules': 100,
                    'max_api_keys': 50
                }
            ),
            'enterprise': PricingTier(
                tier_id='enterprise',
                name='Enterprise',
                plan_type=PlanType.ENTERPRISE,
                base_price_monthly=Decimal('99900'),  # ¥99,900/month
                base_price_yearly=Decimal('999000'),   # ¥999,000/year
                included_usage={
                    MetricType.API_CALLS.value: 10000000,
                    MetricType.COMPUTE_TIME.value: 2000,  # hours
                    MetricType.STORAGE_BYTES.value: 5000 * 1024**3,  # 5TB
                },
                overage_rates={
                    MetricType.API_CALLS.value: Decimal('0.02'),
                    MetricType.COMPUTE_TIME.value: Decimal('30'),
                    MetricType.STORAGE_BYTES.value: Decimal('5'),
                },
                features=['24/7 Support', 'Custom SLA', 'Dedicated Resources', 'White-label Options'],
                limits={
                    'max_users': -1,  # Unlimited
                    'max_modules': -1,
                    'max_api_keys': -1
                }
            )
        }
        
    async def create_subscription(self, tenant_id: str, plan_type: PlanType,
                                billing_period: BillingPeriod = BillingPeriod.MONTHLY,
                                payment_method_id: Optional[str] = None) -> Subscription:
        """Create a new subscription for a tenant"""
        async with self.db_pool.acquire() as conn:
            # Check if tenant already has an active subscription
            existing = await conn.fetchrow("""
                SELECT * FROM subscriptions 
                WHERE tenant_id = $1 AND is_active = true
            """, uuid.UUID(tenant_id))
            
            if existing:
                # Deactivate existing subscription
                await conn.execute("""
                    UPDATE subscriptions 
                    SET is_active = false, end_date = $1
                    WHERE subscription_id = $2
                """, datetime.utcnow(), existing['subscription_id'])
                
            # Create new subscription
            row = await conn.fetchrow("""
                INSERT INTO subscriptions (tenant_id, plan_type, billing_period, 
                                         start_date, payment_method_id)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING *
            """, uuid.UUID(tenant_id), plan_type.value, billing_period.value,
                datetime.utcnow(), payment_method_id)
            
            subscription = Subscription(
                subscription_id=str(row['subscription_id']),
                tenant_id=str(row['tenant_id']),
                plan_type=PlanType(row['plan_type']),
                billing_period=BillingPeriod(row['billing_period']),
                start_date=row['start_date'],
                end_date=row['end_date'],
                is_active=row['is_active'],
                auto_renew=row['auto_renew'],
                payment_method_id=row['payment_method_id'],
                metadata=row['metadata']
            )
            
            logger.info(f"Created subscription {subscription.subscription_id} for tenant {tenant_id}")
            
            return subscription
            
    async def track_usage(self, tenant_id: str, metric_type: MetricType, value: Decimal,
                         user_id: Optional[str] = None, module_id: Optional[str] = None,
                         metadata: Optional[Dict[str, Any]] = None):
        """Track usage event"""
        event = UsageEvent(
            event_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            user_id=user_id,
            metric_type=metric_type,
            value=value,
            timestamp=datetime.utcnow(),
            module_id=module_id,
            metadata=metadata or {}
        )
        
        # Add to buffer for batch processing
        self.usage_buffer.append(event)
        
        # Flush if buffer is full
        if len(self.usage_buffer) >= self.buffer_size:
            await self._flush_usage_buffer()
            
        # Update real-time counter in Redis
        await self._update_usage_counter(tenant_id, metric_type, value)
        
    async def _update_usage_counter(self, tenant_id: str, metric_type: MetricType, value: Decimal):
        """Update real-time usage counter in Redis"""
        key = f"usage:{tenant_id}:{metric_type.value}:{datetime.now().strftime('%Y-%m')}"
        await self.redis_client.incrbyfloat(key, float(value))
        await self.redis_client.expire(key, 86400 * 35)  # Expire after 35 days
        
    async def _flush_usage_buffer(self):
        """Flush usage buffer to database"""
        if not self.usage_buffer:
            return
            
        events = self.usage_buffer[:self.buffer_size]
        self.usage_buffer = self.usage_buffer[self.buffer_size:]
        
        async with self.db_pool.acquire() as conn:
            # Batch insert usage events
            await conn.executemany("""
                INSERT INTO usage_events (event_id, tenant_id, user_id, metric_type, 
                                        value, timestamp, module_id, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """, [(
                uuid.UUID(e.event_id),
                uuid.UUID(e.tenant_id),
                uuid.UUID(e.user_id) if e.user_id else None,
                e.metric_type.value,
                e.value,
                e.timestamp,
                e.module_id,
                json.dumps(e.metadata)
            ) for e in events])
            
        logger.debug(f"Flushed {len(events)} usage events to database")
        
    async def get_usage_summary(self, tenant_id: str, period_start: datetime, 
                               period_end: datetime) -> UsageSummary:
        """Get usage summary for a period"""
        async with self.db_pool.acquire() as conn:
            # Get aggregated usage
            rows = await conn.fetch("""
                SELECT metric_type, SUM(value) as total_value
                FROM usage_events
                WHERE tenant_id = $1 AND timestamp >= $2 AND timestamp < $3
                GROUP BY metric_type
            """, uuid.UUID(tenant_id), period_start, period_end)
            
            usage = {row['metric_type']: Decimal(str(row['total_value'])) for row in rows}
            
            # Calculate cost
            subscription = await self.get_active_subscription(tenant_id)
            total_cost = await self._calculate_usage_cost(usage, subscription)
            
            return UsageSummary(
                tenant_id=tenant_id,
                period_start=period_start,
                period_end=period_end,
                api_calls=int(usage.get(MetricType.API_CALLS.value, 0)),
                compute_hours=usage.get(MetricType.COMPUTE_TIME.value, Decimal('0')),
                storage_gb=usage.get(MetricType.STORAGE_BYTES.value, Decimal('0')) / (1024**3),
                bandwidth_gb=usage.get(MetricType.BANDWIDTH_BYTES.value, Decimal('0')) / (1024**3),
                tokens_used=int(usage.get(MetricType.TOKENS.value, 0)),
                credits_used=usage.get(MetricType.CREDITS.value, Decimal('0')),
                total_cost=total_cost
            )
            
    async def get_active_subscription(self, tenant_id: str) -> Optional[Subscription]:
        """Get active subscription for a tenant"""
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM subscriptions
                WHERE tenant_id = $1 AND is_active = true
            """, uuid.UUID(tenant_id))
            
            if not row:
                return None
                
            return Subscription(
                subscription_id=str(row['subscription_id']),
                tenant_id=str(row['tenant_id']),
                plan_type=PlanType(row['plan_type']),
                billing_period=BillingPeriod(row['billing_period']),
                start_date=row['start_date'],
                end_date=row['end_date'],
                is_active=row['is_active'],
                auto_renew=row['auto_renew'],
                payment_method_id=row['payment_method_id'],
                metadata=row['metadata']
            )
            
    async def _calculate_usage_cost(self, usage: Dict[str, Decimal], 
                                   subscription: Optional[Subscription]) -> Decimal:
        """Calculate cost based on usage and subscription"""
        if not subscription:
            return Decimal('0')
            
        tier = self.pricing_tiers.get(subscription.plan_type.value)
        if not tier:
            return Decimal('0')
            
        total_cost = Decimal('0')
        
        # Calculate overage charges
        for metric, value in usage.items():
            included = Decimal(str(tier.included_usage.get(metric, 0)))
            overage = max(Decimal('0'), value - included)
            
            if overage > 0 and metric in tier.overage_rates:
                rate = tier.overage_rates[metric]
                total_cost += overage * rate
                
        return total_cost
        
    async def generate_invoice(self, tenant_id: str, period_start: datetime, 
                              period_end: datetime, usage_summary: Optional[UsageSummary] = None) -> Invoice:
        """Generate invoice for a billing period"""
        if not usage_summary:
            usage_summary = await self.get_usage_summary(tenant_id, period_start, period_end)
            
        subscription = await self.get_active_subscription(tenant_id)
        if not subscription:
            raise ValueError(f"No active subscription for tenant {tenant_id}")
            
        tier = self.pricing_tiers.get(subscription.plan_type.value)
        
        # Generate invoice number
        invoice_number = f"INV-{datetime.now().strftime('%Y%m')}-{uuid.uuid4().hex[:8].upper()}"
        
        # Calculate line items
        line_items = []
        subtotal = Decimal('0')
        
        # Base plan fee
        if tier.base_price_monthly > 0:
            if subscription.billing_period == BillingPeriod.MONTHLY:
                base_price = tier.base_price_monthly
            else:
                base_price = tier.base_price_yearly / 12
                
            line_items.append({
                'description': f'Democratic AI {tier.name} Plan - Base Fee',
                'quantity': 1,
                'unit_price': float(base_price),
                'amount': float(base_price)
            })
            subtotal += base_price
            
        # Overage charges
        if tier.overage_rates:
            # API calls overage
            if MetricType.API_CALLS.value in tier.overage_rates:
                included = tier.included_usage.get(MetricType.API_CALLS.value, 0)
                overage = max(0, usage_summary.api_calls - included)
                
                if overage > 0:
                    rate = tier.overage_rates[MetricType.API_CALLS.value]
                    amount = overage * rate
                    line_items.append({
                        'description': 'API Calls - Overage',
                        'quantity': overage,
                        'unit_price': float(rate),
                        'amount': float(amount)
                    })
                    subtotal += amount
                    
            # Compute time overage
            if MetricType.COMPUTE_TIME.value in tier.overage_rates:
                included = Decimal(str(tier.included_usage.get(MetricType.COMPUTE_TIME.value, 0)))
                overage = max(Decimal('0'), usage_summary.compute_hours - included)
                
                if overage > 0:
                    rate = tier.overage_rates[MetricType.COMPUTE_TIME.value]
                    amount = overage * rate
                    line_items.append({
                        'description': 'Compute Time - Overage',
                        'quantity': float(overage),
                        'unit_price': float(rate),
                        'amount': float(amount)
                    })
                    subtotal += amount
                    
        # Calculate tax (10% Japanese consumption tax)
        tax = subtotal * Decimal('0.10')
        total = subtotal + tax
        
        # Create invoice
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO invoices (tenant_id, invoice_number, period_start, period_end,
                                    subtotal, tax, total, status, due_date, line_items)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                RETURNING *
            """, uuid.UUID(tenant_id), invoice_number, period_start, period_end,
                subtotal, tax, total, InvoiceStatus.PENDING.value,
                datetime.now() + timedelta(days=30), json.dumps(line_items))
            
            invoice = Invoice(
                invoice_id=str(row['invoice_id']),
                tenant_id=str(row['tenant_id']),
                invoice_number=row['invoice_number'],
                period_start=row['period_start'],
                period_end=row['period_end'],
                subtotal=row['subtotal'],
                tax=row['tax'],
                total=row['total'],
                status=InvoiceStatus(row['status']),
                due_date=row['due_date'],
                paid_at=row['paid_at'],
                line_items=row['line_items'],
                metadata=row['metadata']
            )
            
            logger.info(f"Generated invoice {invoice_number} for tenant {tenant_id}, total: ¥{total}")
            
            return invoice
            
    async def process_payment(self, invoice_id: str, payment_method_id: str) -> PaymentInfo:
        """Process payment for an invoice"""
        async with self.db_pool.acquire() as conn:
            # Get invoice
            invoice_row = await conn.fetchrow("""
                SELECT * FROM invoices WHERE invoice_id = $1
            """, uuid.UUID(invoice_id))
            
            if not invoice_row:
                raise ValueError(f"Invoice {invoice_id} not found")
                
            if invoice_row['status'] == InvoiceStatus.PAID.value:
                raise ValueError(f"Invoice {invoice_id} is already paid")
                
            # Process payment through Stripe
            try:
                payment_intent = stripe.PaymentIntent.create(
                    amount=int(invoice_row['total'] * 100),  # Convert to cents
                    currency='jpy',
                    payment_method=payment_method_id,
                    confirm=True,
                    metadata={
                        'invoice_id': invoice_id,
                        'tenant_id': str(invoice_row['tenant_id'])
                    }
                )
                
                # Record payment
                payment_row = await conn.fetchrow("""
                    INSERT INTO payments (tenant_id, invoice_id, amount, currency, 
                                        payment_method, status, reference)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    RETURNING *
                """, invoice_row['tenant_id'], invoice_row['invoice_id'],
                    invoice_row['total'], 'JPY', PaymentMethod.CREDIT_CARD.value,
                    payment_intent.status, payment_intent.id)
                    
                # Update invoice status
                await conn.execute("""
                    UPDATE invoices 
                    SET status = $1, paid_at = $2
                    WHERE invoice_id = $3
                """, InvoiceStatus.PAID.value, datetime.utcnow(), invoice_row['invoice_id'])
                
                payment = PaymentInfo(
                    payment_id=str(payment_row['payment_id']),
                    tenant_id=str(payment_row['tenant_id']),
                    amount=payment_row['amount'],
                    currency=payment_row['currency'],
                    payment_method=PaymentMethod(payment_row['payment_method']),
                    status=payment_row['status'],
                    processed_at=payment_row['processed_at'],
                    reference=payment_row['reference'],
                    metadata=payment_row['metadata']
                )
                
                logger.info(f"Payment processed successfully for invoice {invoice_id}")
                
                return payment
                
            except stripe.error.StripeError as e:
                logger.error(f"Payment failed for invoice {invoice_id}: {e}")
                raise ValueError(f"Payment processing failed: {e}")
                
    async def add_payment_method(self, tenant_id: str, stripe_payment_method_id: str) -> str:
        """Add a payment method for a tenant"""
        # Retrieve payment method details from Stripe
        pm = stripe.PaymentMethod.retrieve(stripe_payment_method_id)
        
        async with self.db_pool.acquire() as conn:
            # Check if this is the first payment method
            existing = await conn.fetchrow("""
                SELECT COUNT(*) as count FROM payment_methods
                WHERE tenant_id = $1
            """, uuid.UUID(tenant_id))
            
            is_default = existing['count'] == 0
            
            # Save payment method
            row = await conn.fetchrow("""
                INSERT INTO payment_methods (tenant_id, type, stripe_payment_method_id,
                                           last4, brand, is_default)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING method_id
            """, uuid.UUID(tenant_id), pm.type, stripe_payment_method_id,
                pm.card.last4 if pm.card else None,
                pm.card.brand if pm.card else None,
                is_default)
                
            logger.info(f"Added payment method for tenant {tenant_id}")
            
            return str(row['method_id'])
            
    async def check_rate_limit(self, tenant_id: str, metric_type: MetricType) -> tuple[bool, int]:
        """Check if tenant has exceeded rate limit"""
        subscription = await self.get_active_subscription(tenant_id)
        if not subscription:
            return False, 0
            
        tier = self.pricing_tiers.get(subscription.plan_type.value)
        if not tier:
            return True, 0
            
        # Get current usage from Redis
        key = f"usage:{tenant_id}:{metric_type.value}:{datetime.now().strftime('%Y-%m-%d-%H')}"
        current = await self.redis_client.get(key)
        current_value = int(float(current)) if current else 0
        
        # Get limit for this metric
        limit = tier.limits.get(f"hourly_{metric_type.value}", float('inf'))
        
        if current_value >= limit:
            return False, 0
            
        return True, limit - current_value
        
    async def _usage_aggregator(self):
        """Background task to aggregate usage data"""
        while True:
            try:
                # Flush buffer periodically
                if self.usage_buffer:
                    await self._flush_usage_buffer()
                    
                # Aggregate daily usage
                await self._aggregate_daily_usage()
                
                await asyncio.sleep(self.flush_interval)
                
            except Exception as e:
                logger.error(f"Error in usage aggregator: {e}")
                await asyncio.sleep(10)
                
    async def _aggregate_daily_usage(self):
        """Aggregate usage data daily"""
        yesterday = date.today() - timedelta(days=1)
        
        async with self.db_pool.acquire() as conn:
            # Get tenants to aggregate
            tenants = await conn.fetch("""
                SELECT DISTINCT tenant_id FROM usage_events
                WHERE DATE(timestamp) = $1
            """, yesterday)
            
            for tenant_row in tenants:
                tenant_id = tenant_row['tenant_id']
                
                # Aggregate by metric type
                await conn.execute("""
                    INSERT INTO usage_aggregates (tenant_id, date, metric_type, total_value, count)
                    SELECT tenant_id, DATE(timestamp), metric_type, SUM(value), COUNT(*)
                    FROM usage_events
                    WHERE tenant_id = $1 AND DATE(timestamp) = $2
                    GROUP BY tenant_id, DATE(timestamp), metric_type
                    ON CONFLICT (tenant_id, date, metric_type) 
                    DO UPDATE SET total_value = EXCLUDED.total_value, count = EXCLUDED.count
                """, tenant_id, yesterday)
                
    async def _invoice_generator(self):
        """Background task to generate invoices"""
        while True:
            try:
                # Check for subscriptions that need invoicing
                today = date.today()
                
                if today.day == 1:  # First day of month
                    await self._generate_monthly_invoices()
                    
                await asyncio.sleep(3600)  # Check every hour
                
            except Exception as e:
                logger.error(f"Error in invoice generator: {e}")
                await asyncio.sleep(3600)
                
    async def _generate_monthly_invoices(self):
        """Generate monthly invoices for all active subscriptions"""
        last_month_start = datetime.now().replace(day=1) - timedelta(days=1)
        last_month_start = last_month_start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        last_month_end = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        async with self.db_pool.acquire() as conn:
            # Get active subscriptions
            subscriptions = await conn.fetch("""
                SELECT * FROM subscriptions
                WHERE is_active = true AND billing_period = $1
            """, BillingPeriod.MONTHLY.value)
            
            for sub_row in subscriptions:
                tenant_id = str(sub_row['tenant_id'])
                
                try:
                    # Check if invoice already exists
                    existing = await conn.fetchrow("""
                        SELECT * FROM invoices
                        WHERE tenant_id = $1 AND period_start = $2
                    """, sub_row['tenant_id'], last_month_start)
                    
                    if not existing:
                        # Generate invoice
                        await self.generate_invoice(tenant_id, last_month_start, last_month_end)
                        
                except Exception as e:
                    logger.error(f"Failed to generate invoice for tenant {tenant_id}: {e}")
                    
    async def _payment_processor(self):
        """Background task to process auto-payments"""
        while True:
            try:
                # Process auto-payments for overdue invoices
                await self._process_auto_payments()
                
                await asyncio.sleep(3600 * 6)  # Check every 6 hours
                
            except Exception as e:
                logger.error(f"Error in payment processor: {e}")
                await asyncio.sleep(3600)
                
    async def _process_auto_payments(self):
        """Process auto-payments for subscriptions"""
        async with self.db_pool.acquire() as conn:
            # Get overdue invoices with auto-renew subscriptions
            invoices = await conn.fetch("""
                SELECT i.*, s.payment_method_id
                FROM invoices i
                JOIN subscriptions s ON i.tenant_id = s.tenant_id
                WHERE i.status = $1 AND i.due_date < $2 
                AND s.auto_renew = true AND s.payment_method_id IS NOT NULL
            """, InvoiceStatus.PENDING.value, datetime.utcnow())
            
            for invoice_row in invoices:
                try:
                    # Get default payment method
                    pm = await conn.fetchrow("""
                        SELECT * FROM payment_methods
                        WHERE tenant_id = $1 AND is_default = true
                    """, invoice_row['tenant_id'])
                    
                    if pm:
                        # Process payment
                        await self.process_payment(
                            str(invoice_row['invoice_id']),
                            pm['stripe_payment_method_id']
                        )
                        
                except Exception as e:
                    logger.error(f"Auto-payment failed for invoice {invoice_row['invoice_id']}: {e}")
                    
    async def cleanup(self):
        """Clean up resources"""
        if self.db_pool:
            await self.db_pool.close()
        if self.redis_client:
            await self.redis_client.close()


# Example usage
async def main():
    config = {
        'stripe_api_key': 'sk_test_...',  # Your Stripe API key
        'db_host': 'localhost',
        'db_port': 5435,
        'db_name': 'democratic_ai_billing',
        'db_user': 'postgres',
        'db_password': 'password',
        'redis_url': 'redis://localhost:6379/1'
    }
    
    billing_service = BillingService(config)
    await billing_service.initialize()
    
    # Create subscription
    subscription = await billing_service.create_subscription(
        tenant_id='test-tenant-id',
        plan_type=PlanType.STARTER,
        billing_period=BillingPeriod.MONTHLY
    )
    
    # Track usage
    await billing_service.track_usage(
        tenant_id='test-tenant-id',
        metric_type=MetricType.API_CALLS,
        value=Decimal('100')
    )
    
    await billing_service.cleanup()


if __name__ == "__main__":
    asyncio.run(main())

import os
import json
import time
import random
import hashlib
import logging
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional, Any

import httpx
import redis.asyncio as redis

from fastapi import FastAPI, APIRouter, HTTPException, Query, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# ============================================================
# CONFIGURATION
# ============================================================

SERVICE_NAME = "Proxy Manager API"
VERSION = "1.0.0"

SOURCE_URL = os.getenv(
    "PROXY_SOURCE_URL",
    "http://localhost:8080/proxies.json"
)

REDIS_URL = os.getenv(
    "REDIS_URL",
    "redis://localhost:6379/0"
)

API_KEY = os.getenv("API_KEY", "")

CACHE_UPDATE_INTERVAL = int(
    os.getenv("CACHE_UPDATE_INTERVAL", "300")
)

DEFAULT_LEASE_TTL = 600
MAX_LEASE_TTL = 3600

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logger = logging.getLogger("proxy-manager")


# ============================================================
# GLOBAL STATE
# ============================================================

proxy_cache: list[dict] = []
proxy_index: dict[str, dict] = {}

redis_client: Optional[redis.Redis] = None
http_client: Optional[httpx.AsyncClient] = None

last_refresh: Optional[str] = None
last_refresh_timestamp: Optional[float] = None

refresh_lock = asyncio.Lock()


# ============================================================
# HELPERS
# ============================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def get_proxy_id(proxy: dict) -> str:
    raw = (
        f"{proxy.get('protocol')}:"
        f"{proxy.get('host')}:"
        f"{proxy.get('port')}"
    )

    return hashlib.sha256(raw.encode()).hexdigest()


def get_country(proxy: dict) -> Optional[str]:
    geo = proxy.get("geolocation") or {}
    country = geo.get("country") or {}

    return country.get("iso_code")


def get_city(proxy: dict) -> Optional[str]:
    geo = proxy.get("geolocation") or {}
    city = geo.get("city") or {}
    names = city.get("names") or {}

    return names.get("en")


def normalize_proxy(proxy: dict) -> dict:
    result = dict(proxy)

    result["id"] = get_proxy_id(proxy)
    result["country"] = get_country(proxy)
    result["city"] = get_city(proxy)

    return result


def filter_proxies(
    country=None,
    protocol=None,
    max_timeout=None,
    min_timeout=None
):
    result = proxy_cache

    if country:
        country = country.upper()

        result = [
            p for p in result
            if p.get("country") == country
        ]

    if protocol:
        protocol = protocol.lower()

        result = [
            p for p in result
            if p.get("protocol", "").lower() == protocol
        ]

    if max_timeout is not None:
        result = [
            p for p in result
            if p.get("timeout") is not None
            and p["timeout"] <= max_timeout
        ]

    if min_timeout is not None:
        result = [
            p for p in result
            if p.get("timeout") is not None
            and p["timeout"] >= min_timeout
        ]

    return result


# ============================================================
# AUTHENTICATION
# ============================================================

async def check_api_key(
    x_api_key: Optional[str] = Header(default=None)
):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Invalid API key"
        )


# ============================================================
# REDIS LUA SCRIPTS
# ============================================================

# Atomic proxy reservation.
#
# KEYS[1] = user lease key
# KEYS[2] = proxy reservation key
#
# ARGV[1] = serialized proxy
# ARGV[2] = user id
# ARGV[3] = TTL

LEASE_SCRIPT = """
local existing = redis.call('GET', KEYS[1])

if existing then
    return { 'EXISTING', existing }
end

local reserved = redis.call('GET', KEYS[2])

if reserved then
    return { 'BUSY', reserved }
end

redis.call(
    'SET',
    KEYS[2],
    ARGV[2],
    'EX',
    ARGV[3]
)

redis.call(
    'SET',
    KEYS[1],
    ARGV[1],
    'EX',
    ARGV[3]
)

return { 'CREATED', ARGV[1] }
"""


# Atomic lease renewal.

RENEW_SCRIPT = """
local lease = redis.call('GET', KEYS[1])

if not lease then
    return { 'NOT_FOUND', '' }
end

local proxy = cjson.decode(lease)
local owner = redis.call('GET', KEYS[2])

if owner ~= ARGV[1] then
    return { 'INVALID', '' }
end

redis.call('EXPIRE', KEYS[1], ARGV[2])
redis.call('EXPIRE', KEYS[2], ARGV[2])

return { 'RENEWED', lease }
"""


# Atomic lease release.

RELEASE_SCRIPT = """
local lease = redis.call('GET', KEYS[1])

if not lease then
    return 0
end

local proxy = cjson.decode(lease)
local owner = redis.call('GET', KEYS[2])

if owner ~= ARGV[1] then
    return 0
end

redis.call('DEL', KEYS[1])
redis.call('DEL', KEYS[2])

return 1
"""


# ============================================================
# REDIS KEYS
# ============================================================

def user_lease_key(user_id: str):
    user_hash = hashlib.sha256(
        user_id.encode()
    ).hexdigest()

    return f"proxy:lease:user:{user_hash}"


def proxy_reservation_key(proxy_id: str):
    return f"proxy:reserved:{proxy_id}"


# ============================================================
# DATA SOURCE
# ============================================================

async def refresh_proxies():
    global proxy_cache
    global proxy_index
    global last_refresh
    global last_refresh_timestamp

    async with refresh_lock:

        logger.info("Refreshing proxy list from %s", SOURCE_URL)

        try:
            response = await http_client.get(
                SOURCE_URL,
                timeout=30
            )

            response.raise_for_status()

            data = response.json()

            if not isinstance(data, list):
                raise ValueError("Expected JSON array")

            normalized = []

            for item in data:
                if not isinstance(item, dict):
                    continue

                if not item.get("host"):
                    continue

                if not item.get("port"):
                    continue

                if not item.get("protocol"):
                    continue

                normalized.append(
                    normalize_proxy(item)
                )

            if not normalized:
                raise ValueError("Empty proxy list")

            # Replace cache only after successful parsing.
            new_index = {
                p["id"]: p
                for p in normalized
            }

            proxy_cache = normalized
            proxy_index = new_index

            last_refresh = now_iso()
            last_refresh_timestamp = time.time()

            logger.info(
                "Proxy cache updated: %s proxies",
                len(proxy_cache)
            )

        except Exception as e:
            logger.exception(
                "Proxy list refresh failed: %s",
                e
            )


async def refresh_worker():
    while True:
        await asyncio.sleep(CACHE_UPDATE_INTERVAL)
        await refresh_proxies()


# ============================================================
# APPLICATION LIFECYCLE
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client
    global http_client

    logger.info("Starting Proxy Manager API")

    redis_client = redis.from_url(
        REDIS_URL,
        decode_responses=True
    )

    await redis_client.ping()

    http_client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=30
    )

    await refresh_proxies()

    worker = asyncio.create_task(
        refresh_worker()
    )

    yield

    worker.cancel()

    try:
        await worker
    except asyncio.CancelledError:
        pass

    await http_client.aclose()
    await redis_client.aclose()

    logger.info("Proxy Manager API stopped")


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title=SERVICE_NAME,
    description="""
# Proxy Manager API

Микросервис управления пулом прокси.

## Возможности

- Получение полного списка прокси.
- Фильтрация по странам.
- Фильтрация по протоколам.
- Случайная выдача.
- Временное закрепление за пользователем.
- Продление аренды.
- Освобождение прокси.
- Проверка состояния аренды.
- Статистика.

## Источник

monosans/proxy-list

## Хранилище

Redis
""",
    version=VERSION,
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(check_api_key)]
)


# ============================================================
# PYDANTIC MODELS
# ============================================================

class LeaseRequest(BaseModel):
    user_id: str = Field(
        min_length=1,
        max_length=128,
        description="Уникальный идентификатор пользователя"
    )

    country: Optional[str] = Field(
        default=None,
        description="ISO-код страны, например US, DE, FI"
    )

    protocol: Optional[str] = Field(
        default=None,
        description="http, socks4 или socks5"
    )

    ttl: int = Field(
        default=DEFAULT_LEASE_TTL,
        ge=60,
        le=MAX_LEASE_TTL,
        description="Время аренды в секундах"
    )

    max_timeout: Optional[float] = Field(
        default=None,
        gt=0,
        description="Максимальное время проверки прокси"
    )


class RenewRequest(BaseModel):
    ttl: int = Field(
        default=DEFAULT_LEASE_TTL,
        ge=60,
        le=MAX_LEASE_TTL
    )


# ============================================================
# HEALTH
# ============================================================

@app.get(
    "/health",
    tags=["System"]
)
async def health():
    redis_ok = False

    try:
        redis_ok = await redis_client.ping()
    except Exception:
        pass

    return {
        "status": "ok" if redis_ok else "degraded",
        "redis": redis_ok,
        "proxy_count": len(proxy_cache),
        "last_refresh": last_refresh
    }


# ============================================================
# PROXY LIST
# ============================================================

@router.get(
    "/proxies",
    tags=["Proxies"],
    summary="Получить список прокси"
)
async def get_proxies(
    country: Optional[str] = Query(None),
    protocol: Optional[str] = Query(None),
    min_timeout: Optional[float] = Query(None, ge=0),
    max_timeout: Optional[float] = Query(None, gt=0),
    limit: int = Query(100, ge=1, le=10000),
    offset: int = Query(0, ge=0)
):
    result = filter_proxies(
        country=country,
        protocol=protocol,
        min_timeout=min_timeout,
        max_timeout=max_timeout
    )

    total = len(result)

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": result[offset:offset + limit],
        "last_refresh": last_refresh
    }


# ============================================================
# RANDOM PROXY
# ============================================================

@router.get(
    "/proxies/random",
    tags=["Proxies"],
    summary="Получить случайный прокси"
)
async def random_proxy(
    country: Optional[str] = Query(None),
    protocol: Optional[str] = Query(None),
    max_timeout: Optional[float] = Query(None, gt=0)
):
    result = filter_proxies(
        country=country,
        protocol=protocol,
        max_timeout=max_timeout
    )

    if not result:
        raise HTTPException(
            status_code=404,
            detail="No proxies found"
        )

    return {
        "proxy": random.choice(result),
        "total_available": len(result)
    }


# ============================================================
# LEASE PROXY
# ============================================================

@router.post(
    "/proxies/lease",
    tags=["Leases"],
    summary="Закрепить прокси за пользователем"
)
async def lease_proxy(request: LeaseRequest):

    user_key = user_lease_key(request.user_id)

    # Existing lease always takes priority.
    existing = await redis_client.get(user_key)

    if existing:
        proxy = json.loads(existing)

        return {
            "success": True,
            "created": False,
            "message": "Existing lease returned",
            "user_id": request.user_id,
            "proxy": proxy,
            "ttl": await redis_client.ttl(user_key)
        }

    candidates = filter_proxies(
        country=request.country,
        protocol=request.protocol,
        max_timeout=request.max_timeout
    )

    if not candidates:
        raise HTTPException(
            status_code=404,
            detail="No matching proxies available"
        )

    # Randomize candidates to distribute assignments.
    candidates = random.sample(
        candidates,
        len(candidates)
    )

    for proxy in candidates:

        proxy_id = proxy["id"]

        reservation_key = proxy_reservation_key(
            proxy_id
        )

        payload = json.dumps(
            proxy,
            ensure_ascii=False
        )

        result = await redis_client.eval(
            LEASE_SCRIPT,
            2,
            user_key,
            reservation_key,
            payload,
            request.user_id,
            request.ttl
        )

        status = result[0]

        if status == "EXISTING":
            return {
                "success": True,
                "created": False,
                "user_id": request.user_id,
                "proxy": json.loads(result[1]),
                "ttl": await redis_client.ttl(user_key)
            }

        if status == "CREATED":
            return {
                "success": True,
                "created": True,
                "user_id": request.user_id,
                "proxy": json.loads(result[1]),
                "ttl": request.ttl,
                "expires_at": datetime.fromtimestamp(
                    time.time() + request.ttl,
                    timezone.utc
                ).isoformat()
            }

    raise HTTPException(
        status_code=503,
        detail="No free proxies available"
    )


# ============================================================
# GET USER LEASE
# ============================================================

@router.get(
    "/proxies/lease/{user_id}",
    tags=["Leases"],
    summary="Получить прокси пользователя"
)
async def get_user_lease(user_id: str):

    user_key = user_lease_key(user_id)

    lease = await redis_client.get(user_key)

    if not lease:
        raise HTTPException(
            status_code=404,
            detail="Lease not found"
        )

    return {
        "success": True,
        "user_id": user_id,
        "proxy": json.loads(lease),
        "ttl": await redis_client.ttl(user_key)
    }


# ============================================================
# LEASE STATUS
# ============================================================

@router.get(
    "/proxies/lease/{user_id}/status",
    tags=["Leases"],
    summary="Проверить состояние аренды"
)
async def lease_status(user_id: str):

    user_key = user_lease_key(user_id)

    lease = await redis_client.get(user_key)

    if not lease:
        return {
            "active": False,
            "user_id": user_id
        }

    ttl = await redis_client.ttl(user_key)

    return {
        "active": ttl > 0,
        "user_id": user_id,
        "proxy": json.loads(lease),
        "ttl": ttl
    }


# ============================================================
# RENEW LEASE
# ============================================================

@router.post(
    "/proxies/lease/{user_id}/renew",
    tags=["Leases"],
    summary="Продлить аренду прокси"
)
async def renew_lease(
    user_id: str,
    request: RenewRequest
):

    user_key = user_lease_key(user_id)

    lease = await redis_client.get(user_key)

    if not lease:
        raise HTTPException(
            status_code=404,
            detail="Lease not found"
        )

    proxy = json.loads(lease)

    reservation_key = proxy_reservation_key(
        proxy["id"]
    )

    result = await redis_client.eval(
        RENEW_SCRIPT,
        2,
        user_key,
        reservation_key,
        user_id,
        request.ttl
    )

    if result[0] != "RENEWED":
        raise HTTPException(
            status_code=409,
            detail="Lease reservation is invalid"
        )

    return {
        "success": True,
        "user_id": user_id,
        "proxy": json.loads(result[1]),
        "ttl": request.ttl,
        "expires_at": datetime.fromtimestamp(
            time.time() + request.ttl,
            timezone.utc
        ).isoformat()
    }


# ============================================================
# RELEASE LEASE
# ============================================================

@router.delete(
    "/proxies/lease/{user_id}",
    tags=["Leases"],
    summary="Освободить прокси"
)
async def release_lease(user_id: str):

    user_key = user_lease_key(user_id)

    lease = await redis_client.get(user_key)

    if not lease:
        return {
            "success": True,
            "released": False,
            "message": "No active lease"
        }

    proxy = json.loads(lease)

    reservation_key = proxy_reservation_key(
        proxy["id"]
    )

    result = await redis_client.eval(
        RELEASE_SCRIPT,
        2,
        user_key,
        reservation_key,
        user_id
    )

    return {
        "success": True,
        "released": bool(result),
        "user_id": user_id
    }


# ============================================================
# STATISTICS
# ============================================================

@router.get(
    "/proxies/stats",
    tags=["Statistics"],
    summary="Статистика пула прокси"
)
async def proxy_stats():

    protocols = {}
    countries = {}

    for proxy in proxy_cache:

        protocol = proxy.get("protocol", "unknown")

        protocols[protocol] = (
            protocols.get(protocol, 0) + 1
        )

        country = proxy.get("country")

        if country:
            countries[country] = (
                countries.get(country, 0) + 1
            )

    return {
        "total": len(proxy_cache),
        "protocols": protocols,
        "countries": countries,
        "last_refresh": last_refresh,
        "cache_update_interval": CACHE_UPDATE_INTERVAL
    }


# ============================================================
# MANUAL CACHE REFRESH
# ============================================================

@router.post(
    "/proxies/refresh",
    tags=["System"],
    summary="Обновить список прокси вручную"
)
async def manual_refresh():

    await refresh_proxies()

    return {
        "success": last_refresh_timestamp is not None,
        "total": len(proxy_cache),
        "last_refresh": last_refresh
    }


# ============================================================
# REGISTER ROUTER
# ============================================================

app.include_router(router)


# ============================================================
# ROOT
# ============================================================

@app.get("/", tags=["System"])
async def root():
    return {
        "service": SERVICE_NAME,
        "version": VERSION,
        "docs": "/docs",
        "redoc": "/redoc",
        "health": "/health",
        "source": SOURCE_URL
    }

# ============================================================
# IP CHECK
# ============================================================

@router.post(
    "/proxies/check-ip",
    tags=["Proxies"],
    summary="Проверить IP через прокси"
)
async def check_ip_via_proxy(
    user_id: Optional[str] = Query(
        None,
        description="ID пользователя для использования арендованного прокси"
    ),
    country: Optional[str] = Query(None),
    protocol: Optional[str] = Query(None)
):
    proxy_to_use = None

    if user_id:
        user_key = user_lease_key(user_id)
        lease = await redis_client.get(user_key)
        
        if lease:
            proxy_to_use = json.loads(lease)
        else:
            raise HTTPException(
                status_code=404,
                detail=f"No active lease for user {user_id}"
            )
    else:
        candidates = filter_proxies(
            country=country,
            protocol=protocol
        )
        
        if not candidates:
            raise HTTPException(
                status_code=404,
                detail="No proxies available"
            )
        
        proxy_to_use = random.choice(candidates)

    proxy_scheme = proxy_to_use.get("protocol", "http")
    proxy_host = proxy_to_use.get("host")
    proxy_port = proxy_to_use.get("port")
    proxy_user = proxy_to_use.get("username")
    proxy_pass = proxy_to_use.get("password")

    if proxy_user and proxy_pass:
        proxy_url = f"{proxy_scheme}://{proxy_user}:{proxy_pass}@{proxy_host}:{proxy_port}"
    else:
        proxy_url = f"{proxy_scheme}://{proxy_host}:{proxy_port}"

    try:
        # ИСПРАВЛЕНО: proxy вместо proxies
        async with httpx.AsyncClient(
            proxy=proxy_url,
            timeout=10.0
        ) as client:
            responses = {}
            
            try:
                r = await client.get("https://httpbin.org/ip")
                responses["httpbin"] = r.json()
            except Exception as e:
                responses["httpbin"] = {"error": str(e)}
            
            try:
                r = await client.get("http://ip-api.com/json/?fields=status,message,country,countryCode,region,regionName,city,zip,lat,lon,timezone,isp,org,as,query")
                responses["ip-api"] = r.json()
            except Exception as e:
                responses["ip-api"] = {"error": str(e)}

            return {
                "success": True,
                "proxy": {
                    "id": proxy_to_use.get("id"),
                    "protocol": proxy_scheme,
                    "host": proxy_host,
                    "port": proxy_port,
                    "country": proxy_to_use.get("country"),
                    "city": proxy_to_use.get("city")
                },
                "external_ip_info": responses,
                "checked_at": now_iso()
            }

    except httpx.ProxyError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Proxy connection failed: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Check failed: {str(e)}"
        )


@router.get(
    "/proxies/check-ip/simple",
    tags=["Proxies"],
    summary="Простая проверка IP (только внешний адрес)"
)
async def check_ip_simple(
    user_id: Optional[str] = Query(None),
    country: Optional[str] = Query(None),
    protocol: Optional[str] = Query(None)
):
    proxy_to_use = None

    if user_id:
        user_key = user_lease_key(user_id)
        lease = await redis_client.get(user_key)
        
        if not lease:
            raise HTTPException(
                status_code=404,
                detail=f"No active lease for user {user_id}"
            )
        
        proxy_to_use = json.loads(lease)
    else:
        candidates = filter_proxies(
            country=country,
            protocol=protocol
        )
        
        if not candidates:
            raise HTTPException(
                status_code=404,
                detail="No proxies available"
            )
        
        proxy_to_use = random.choice(candidates)

    proxy_scheme = proxy_to_use.get("protocol", "http")
    proxy_host = proxy_to_use.get("host")
    proxy_port = proxy_to_use.get("port")
    proxy_user = proxy_to_use.get("username")
    proxy_pass = proxy_to_use.get("password")

    if proxy_user and proxy_pass:
        proxy_url = f"{proxy_scheme}://{proxy_user}:{proxy_pass}@{proxy_host}:{proxy_port}"
    else:
        proxy_url = f"{proxy_scheme}://{proxy_host}:{proxy_port}"

    try:
        # ИСПРАВЛЕНО: proxy вместо proxies
        async with httpx.AsyncClient(
            proxy=proxy_url,
            timeout=10.0
        ) as client:
            r = await client.get("https://api.ipify.org?format=json")
            data = r.json()
            
            return {
                "success": True,
                "external_ip": data.get("ip"),
                "proxy": {
                    "host": proxy_host,
                    "port": proxy_port,
                    "country": proxy_to_use.get("country")
                }
            }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Check failed: {str(e)}"
        )
        
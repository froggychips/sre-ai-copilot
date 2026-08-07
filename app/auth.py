from typing import Annotated, List

import structlog
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt
from jwt.exceptions import InvalidTokenError
from pydantic import BaseModel, Field

from app.config import settings

# Initialize logger
logger = structlog.get_logger()

# Security scheme for Swagger UI
security = HTTPBearer()

# Закрытый список АСИММЕТРИЧНЫХ алгоритмов подписи. JWT_PUBLIC_KEY — публичный
# ключ; если через env просочится симметричный JWT_ALGORITHM=HS256, PyJWT
# использует этот ПУБЛИЧНЫЙ ключ как HMAC-секрет — и любой, кто его видел,
# сможет чеканить валидные токены. Поэтому алгоритм пиним, а не доверяем env.
_ASYMMETRIC_JWT_ALGORITHMS = frozenset({
    "RS256", "RS384", "RS512",
    "PS256", "PS384", "PS512",
    "ES256", "ES256K", "ES384", "ES512",
    "EdDSA",
})


class User(BaseModel):
    """
    Pydantic model representing the authenticated user.
    Extracted from the JWT payload.
    """

    sub: str = Field(..., description="Subject identifier (e.g., user ID)")
    email: str = Field(..., description="User's email address")
    roles: List[str] = Field(default_factory=list, description="Assigned user roles")


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
) -> User:
    """
    Dependency to validate JWT and return the authenticated User.
    Expects Authorization: Bearer <token>
    """
    token = credentials.credentials
    log = logger.bind(auth_event="jwt_validation")

    # Пин на асимметричное семейство (см. _ASYMMETRIC_JWT_ALGORITHMS):
    # мисконфиг — это отказ в аутентификации, а не повод пропустить токен.
    algorithm = settings.JWT_ALGORITHM
    if algorithm not in _ASYMMETRIC_JWT_ALGORITHMS:
        log.error("auth_misconfigured_algorithm", algorithm=algorithm)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # iss ОБЯЗАТЕЛЕН всегда (иначе токен другого сервиса того же IdP-ключа
    # аутентифицируется здесь); значение сверяется, когда задан JWT_ISSUER.
    # aud требуется и сверяется, когда задан JWT_AUDIENCE.
    expected_issuer = getattr(settings, "JWT_ISSUER", None)
    required_claims = ["exp", "sub", "iss"]
    if settings.JWT_AUDIENCE:
        required_claims.append("aud")

    try:
        # Decode and validate the token
        payload = jwt.decode(
            token,
            settings.JWT_PUBLIC_KEY,  # RSA Public Key string from ENV
            algorithms=[algorithm],
            audience=settings.JWT_AUDIENCE,
            issuer=expected_issuer,
            options={"require": required_claims},
        )

        # sub обязателен — пустую строку всё равно отсекаем дальше через InvalidTokenError.
        # email может быть None в claims; нормализуем в "" для типа.
        user = User(
            sub=payload.get("sub") or "",
            email=payload.get("email") or "",
            roles=payload.get("roles", []),
        )

        if not user.sub:
            raise InvalidTokenError("Missing 'sub' in payload")

        return user

    except InvalidTokenError as e:
        log.warning("auth_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except Exception as e:
        # Fail-closed 401, не 500: PyJWT кидает InvalidKeyError (НЕ подкласс
        # InvalidTokenError) на пустой/битый JWT_PUBLIC_KEY — раньше это
        # утекало 500-кой. Любая неожиданная ошибка валидации = отказ в auth.
        log.error("auth_unexpected_error", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_role(required_role: str):
    """
    Higher-order dependency to enforce role-based access control.
    Example: @app.get("/admin", dependencies=[Depends(require_role("admin"))])
    """

    async def role_checker(user: User = Depends(get_current_user)):
        if required_role not in user.roles:
            logger.warning(
                "rbac_denied", user=user.sub, required=required_role, present=user.roles
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Operation requires '{required_role}' role",
            )
        return user

    return role_checker

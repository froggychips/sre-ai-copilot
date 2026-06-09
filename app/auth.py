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

    try:
        # Decode and validate the token
        # aud проверяется только когда задан JWT_AUDIENCE (иначе не требуем).
        payload = jwt.decode(
            token,
            settings.JWT_PUBLIC_KEY,  # RSA Public Key string from ENV
            algorithms=[settings.JWT_ALGORITHM],
            audience=settings.JWT_AUDIENCE,
            options={"require": ["exp", "sub"]},
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
        log.error("auth_unexpected_error", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal auth error",
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

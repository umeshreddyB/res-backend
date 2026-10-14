import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from .core.security import decode_token
from .db import get_db
from .models import Role, User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token",
                                 headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = decode_token(token)
        user = db.get(User, int(payload["sub"]))
    except (jwt.PyJWTError, KeyError, ValueError):
        raise unauthorized
    if not user or not user.is_active:
        raise unauthorized
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != Role.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    return user

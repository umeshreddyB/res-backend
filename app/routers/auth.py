from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.security import create_access_token, hash_password, verify_password
from ..db import get_db
from ..deps import get_current_user
from ..models import Role, User
from ..schemas import LoginIn, TokenOut, UserCreate, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


def _login(db: Session, email: str, password: str) -> TokenOut:
    user = db.scalar(select(User).where(User.email == email.lower()))
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
    return TokenOut(access_token=create_access_token(user.id, user.role.value), user=user)


@router.post("/register", response_model=UserOut, status_code=201)
def register(data: UserCreate, db: Session = Depends(get_db)):
    email = data.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    user = User(name=data.name, email=email, phone=data.phone,
                password_hash=hash_password(data.password), role=Role.customer)
    db.add(user)
    db.commit()
    return user


@router.post("/login", response_model=TokenOut)
def login(data: LoginIn, db: Session = Depends(get_db)):
    return _login(db, data.email, data.password)


@router.post("/token", response_model=TokenOut, include_in_schema=True)
def token(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """Form-based login (username = email). Used by the Swagger 'Authorize' button."""
    return _login(db, form.username, form.password)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user

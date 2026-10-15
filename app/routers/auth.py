import re

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.rate_limit import get_client_ip, login_limiter, register_limiter
from ..core.security import create_access_token, hash_password, verify_password
from ..db import get_db
from ..deps import get_current_user, require_admin
from ..models import Role, User
from ..schemas import (ChangePasswordIn, CreateStaffIn, LoginIn, TokenOut,
                       UserCreate, UserOut)

router = APIRouter(prefix="/auth", tags=["auth"])

# Indian phone: optional +91, then 10 digits starting with 6-9
_PHONE_RE = re.compile(r"^(\+91)?[6-9]\d{9}$")


def _validate_phone(phone: str | None) -> None:
    if phone and not _PHONE_RE.match(phone.replace(" ", "").replace("-", "")):
        raise HTTPException(400, "Invalid Indian phone number. Expected format: +91XXXXXXXXXX or 10-digit number")


def _login(db: Session, email: str, password: str) -> TokenOut:
    user = db.scalar(select(User).where(User.email == email.lower()))
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
    return TokenOut(access_token=create_access_token(user.id, user.role.value), user=user)


@router.post("/register", response_model=UserOut, status_code=201)
def register(data: UserCreate, request: Request, db: Session = Depends(get_db)):
    register_limiter.check(get_client_ip(request))
    _validate_phone(data.phone)
    email = data.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    user = User(name=data.name, email=email, phone=data.phone,
                password_hash=hash_password(data.password), role=Role.customer)
    db.add(user)
    db.commit()
    return user


@router.post("/login", response_model=TokenOut)
def login(data: LoginIn, request: Request, db: Session = Depends(get_db)):
    login_limiter.check(get_client_ip(request))
    return _login(db, data.email, data.password)


@router.post("/token", response_model=TokenOut, include_in_schema=True)
def token(form: OAuth2PasswordRequestForm = Depends(), request: Request = None,
          db: Session = Depends(get_db)):
    """Form-based login (username = email). Used by the Swagger 'Authorize' button."""
    if request:
        login_limiter.check(get_client_ip(request))
    return _login(db, form.username, form.password)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


@router.post("/change-password", status_code=200)
def change_password(data: ChangePasswordIn, user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    if not verify_password(data.current_password, user.password_hash):
        raise HTTPException(400, "Current password is incorrect")
    user.password_hash = hash_password(data.new_password)
    db.commit()
    return {"detail": "Password changed successfully"}


@router.post("/admin/create-user", response_model=UserOut, status_code=201,
             tags=["admin: auth"], dependencies=[Depends(require_admin)])
def create_staff(data: CreateStaffIn, db: Session = Depends(get_db)):
    """Admin-only: create a staff or admin user."""
    _validate_phone(data.phone)
    email = data.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    user = User(name=data.name, email=email, phone=data.phone,
                password_hash=hash_password(data.password), role=data.role)
    db.add(user)
    db.commit()
    return user

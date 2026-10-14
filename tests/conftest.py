import os

os.environ["DATABASE_URL"] = "sqlite:///./test.db"
os.environ["RAZORPAY_KEY_ID"] = ""
os.environ["RAZORPAY_KEY_SECRET"] = "test-secret"
os.environ["RAZORPAY_WEBHOOK_SECRET"] = "whsec"

import pytest
from fastapi.testclient import TestClient

from app.db import Base, engine
from app.main import app


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
    with TestClient(app) as c:  # runs lifespan: creates tables + seeds admin
        yield c

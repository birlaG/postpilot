from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet():
    return Fernet(settings.FERNET_KEY.encode())


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode() if value else ""


def decrypt(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        return ""

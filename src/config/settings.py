"""Django settings. Everything environment-specific comes from the environment / .env (SoT 14, 14.5)."""
from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")
env = os.environ.get

ENV = env("INWARD_ENV", "dev")  # dev | prod
PROD = ENV == "prod"
DEBUG = not PROD and env("INWARD_DEBUG", "1") == "1"

SECRET_KEY = env("DJANGO_SECRET_KEY") or ("dev-only-insecure-key" if not PROD else "")
if not SECRET_KEY:
    raise RuntimeError("DJANGO_SECRET_KEY must be set in production")

ALLOWED_HOSTS = [h.strip() for h in env("INWARD_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [f"https://{h}" for h in ALLOWED_HOSTS if "." in h and not h[0].isdigit()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_otp",
    "django_otp.plugins.otp_totp",
    "django_otp.plugins.otp_static",
    "axes",
    "inward.accounts",
    "inward.core",
    "inward.web",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "inward.accounts.middleware.TwoFactorRequiredMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "inward.web.middleware.SecurityHeadersMiddleware",
    "axes.middleware.AxesMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
        "inward.web.context.depot",
    ]},
}]


def _database(url: str) -> dict:
    if not url:
        raise RuntimeError("INWARD_DATABASE_URL is not set (see .env.example)")
    u = urlparse(url)
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": u.path.lstrip("/"),
        "USER": unquote(u.username or ""),
        "PASSWORD": unquote(u.password or ""),
        "HOST": u.hostname or "",  # empty = local socket (peer authentication on the server)
        "PORT": str(u.port or 5432),
        "CONN_MAX_AGE": 60,
        "ATOMIC_REQUESTS": False,
        "TEST": {"NAME": env("INWARD_TEST_DATABASE_NAME", "inward_test")},
    }


DATABASES = {"default": _database(env("INWARD_DATABASE_URL", ""))}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------- authentication (SoT 14.5)
AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = ["axes.backends.AxesStandaloneBackend", "django.contrib.auth.backends.ModelBackend"]
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "web:home"

AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = timedelta(minutes=15)
# Lock the account, not the IP: the whole depot shares one public IP, so IP lockout would block everyone.
# IP-level rate limiting is done by Nginx / fail2ban on the server (SoT 14.5).
AXES_LOCKOUT_PARAMETERS = ["username"]
SILENCED_SYSTEM_CHECKS = [
    "axes.W006",
    # HSTS stays limited to this host: includeSubDomains/preload would force every other
    # *.rainbowlogistics.co.in site (accounts, attendance, future ones) onto HTTPS permanently.
    "security.W005",
    "security.W021",
]
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_TEMPLATE = "accounts/locked.html"

OTP_TOTP_ISSUER = "Inward 035 RAINBOW LOGISTICS"
TRUSTED_DEVICE_DAYS = 30

SESSION_COOKIE_AGE = 30 * 60          # 30-minute idle sign-out ...
SESSION_SAVE_EVERY_REQUEST = True     # ... measured from the last request
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CONTENT_TYPE_NOSNIFF = True

if PROD:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000

# ---------------------------------------------------------------- locale
LANGUAGE_CODE = "en-in"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------- static and documents
STATIC_URL = "/static/"
STATIC_ROOT = Path(env("INWARD_STATIC_ROOT", str(BASE_DIR / "var" / "static")))
STATIC_ROOT.mkdir(parents=True, exist_ok=True)
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage" if PROD
                    else "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
DOCUMENT_ROOT = Path(env("INWARD_DOCUMENT_ROOT", str(BASE_DIR / "var" / "documents")))
DATA_UPLOAD_MAX_MEMORY_SIZE = 50 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024

# ---------------------------------------------------------------- application settings
# TEST mode (default): banner on every page, "[TEST]" on every e-mail, and all mail redirected to
# INWARD_TEST_MAIL_TO only. LIVE is set solely on the live server at go-live.
INWARD_MODE = env("INWARD_MODE", "TEST").upper()
if INWARD_MODE not in ("TEST", "LIVE"):
    raise RuntimeError("INWARD_MODE must be TEST or LIVE")

INWARD = {
    "MODE": INWARD_MODE,
    "TEST_MAIL_TO": [a.strip() for a in env("INWARD_TEST_MAIL_TO", "").split(",") if a.strip()],
    "DEPOT_CODE": env("INWARD_DEPOT_CODE", "035"),
    "ADMIN_URL": env("INWARD_ADMIN_URL", "control-035/"),
    "GMAIL_ADDRESS": env("INWARD_GMAIL_ADDRESS", ""),
    "GMAIL_APP_PASSWORD": env("INWARD_GMAIL_APP_PASSWORD", ""),
    "SIT_SUBJECT": env("INWARD_SIT_SUBJECT", "BPIL Depot Stock In Transit Report"),
    "MAIL_CONFIRM_TIMEOUT_MIN": int(env("INWARD_MAIL_CONFIRM_TIMEOUT_MIN", "30")),
    "MESSAGE_ID_DOMAIN": env("INWARD_MESSAGE_ID_DOMAIN", "inward.rainbowlogistics.co.in"),
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO" if PROD else "WARNING"},
}

"""Django settings for the AI news publisher."""

from pathlib import Path

from dotenv import load_dotenv
import os

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def env_int(name: str, default: int) -> int:
    try:
        return int(env_str(name, str(default)))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(env_str(name, str(default)))
    except ValueError:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    return env_str(name, "1" if default else "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in env_str(name, default).split(",") if item.strip()]


SECRET_KEY = env_str(
    "DJANGO_SECRET_KEY",
    "django-insecure-change-me-before-deploying-to-production-0000000",
)

DEBUG = env_bool("DJANGO_DEBUG", True)

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]")

CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

# Trust nginx TLS termination so absolute URLs (sitemap, share links) use https.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sitemaps",
    "news",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "news.context_processors.site_settings",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": {
            "timeout": 30,
            "transaction_mode": "IMMEDIATE",
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
        },
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = env_str("DJANGO_TIME_ZONE", "Asia/Kolkata")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "{asctime} {levelname} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": BASE_DIR / "logs" / "pipeline.log",
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 3,
            "formatter": "verbose",
        },
    },
    "loggers": {
        "news": {
            "handlers": ["console", "file"],
            "level": env_str("LOG_LEVEL", "INFO"),
            "propagate": False,
        },
    },
}

(BASE_DIR / "logs").mkdir(exist_ok=True)


# --- Publication identity -------------------------------------------------

SITE_NAME = env_str("SITE_NAME", "Daily News")
SITE_TAGLINE = env_str("SITE_TAGLINE", "by MobilesHub360.com")
SITE_BASE_URL = env_str("SITE_BASE_URL", "http://127.0.0.1:8000")
SITE_PUBLISHER = env_str("SITE_PUBLISHER", "MobilesHub360.com")
CONTACT_EMAIL = env_str("CONTACT_EMAIL", "support@mobileshub360.com")
PUBLISHER_COUNTRY = env_str("PUBLISHER_COUNTRY", "India")

# Google AdSense. Client script is always loaded when ADSENSE_CLIENT is set.
# Anchor + vignette (page-switch) ads are Auto ads — turn those on in AdSense.
# Slot IDs power manual units between stories / sidebar / article body.
ADSENSE_CLIENT = env_str("ADSENSE_CLIENT", "ca-pub-4652474325650932")
ADSENSE_SLOT_INFEED = env_str("ADSENSE_SLOT_INFEED", "")
ADSENSE_SLOT_SIDEBAR = env_str("ADSENSE_SLOT_SIDEBAR", "")
ADSENSE_SLOT_ARTICLE = env_str("ADSENSE_SLOT_ARTICLE", "")
ADSENSE_INFEED_EVERY = env_int("ADSENSE_INFEED_EVERY", 3)

# Display bylines (one is chosen per article from this list).
ARTICLE_BYLINES = [
    "Aisha Rahman",
    "Benjamin Cole",
    "Priya Nair",
    "Marcus Ellison",
    "Hannah Brooks",
    "Daniel Okonkwo",
    "Sofia Alvarez",
    "James Whitaker",
    "Emily Zhao",
    "Noah Patel",
    "Olivia Grant",
    "Liam Foster",
    "Chloe Bennett",
    "Ethan Morales",
    "Isabella Cruz",
    "Alexander Reed",
    "Mia Thompson",
    "Owen Harper",
    "Ava Sinclair",
    "Lucas Nguyen",
    "Amelia Hughes",
    "Henry Caldwell",
    "Grace Kim",
    "Samuel Ortiz",
    "Ella Morgan",
    "Jack Rivera",
    "Nina Volkov",
    "Caleb Stone",
    "Ruby Lawson",
    "Nathan Pierce",
]


# --- Content pipeline -----------------------------------------------------

TAVILY_API_KEY = env_str("TAVILY_API_KEY")

# Ollama runs the local rewrite model. Keep the model under 4 GB.
OLLAMA_BASE_URL = env_str("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = env_str("OLLAMA_MODEL", "llama3.2:latest")
OLLAMA_TIMEOUT = env_int("OLLAMA_TIMEOUT", 300)
# Must stay constant across every call in a run: Ollama reloads the model when
# num_ctx changes, which costs 30-60s each time on a CPU-only box.
OLLAMA_NUM_CTX = env_int("OLLAMA_NUM_CTX", 8192)
OLLAMA_NUM_PREDICT = env_int("OLLAMA_NUM_PREDICT", 900)
# Keeps the model resident between calls during a run. Overrides the server's
# idle default, so memory is still released once the run finishes.
OLLAMA_KEEP_ALIVE = env_str("OLLAMA_KEEP_ALIVE", "30m")

# How many articles a single daily run should publish.
DAILY_ARTICLE_TARGET = env_int("DAILY_ARTICLE_TARGET", 2)

# Topic discovery breadth: candidates gathered before ranking.
TOPIC_CANDIDATE_POOL = env_int("TOPIC_CANDIDATE_POOL", 60)

# Number of independent sources we try to gather per topic before writing.
SOURCES_PER_TOPIC = env_int("SOURCES_PER_TOPIC", 5)

# Minimum sources required before a rumour-style story may be written at all.
MIN_SOURCES_FOR_RUMOUR = env_int("MIN_SOURCES_FOR_RUMOUR", 2)

# Originality gate: reject drafts that reuse too much source wording.
# Share of the draft's 5-grams that may also appear in a source. 0.40 means
# "at least ~60% original phrasing" — loose enough that factual reporting does
# not trigger rewrite loops, tight enough to catch near-copies.
MAX_NGRAM_OVERLAP = env_float("MAX_NGRAM_OVERLAP", 0.40)
MAX_LONGEST_COMMON_RUN = env_int("MAX_LONGEST_COMMON_RUN", 18)
# When True, a draft that fails the originality threshold is rewritten. When
# False (default), the score is recorded and the draft is stored once — over
# the threshold goes to review, under it can publish.
REWRITE_ON_ORIGINALITY_FAIL = env_bool("REWRITE_ON_ORIGINALITY_FAIL", False)
# Extra LLM safety pass. Rule-based gates always run; this adds ~1 minute/story.
USE_MODEL_SAFETY_REVIEW = env_bool("USE_MODEL_SAFETY_REVIEW", False)
ORIGINALITY_NGRAM_SIZE = env_int("ORIGINALITY_NGRAM_SIZE", 5)

# How many times we re-prompt the model when a gate fails.
MAX_REWRITE_ATTEMPTS = env_int("MAX_REWRITE_ATTEMPTS", 3)

# Publishable article length. Small models cannot hold a long article in one
# response, so the writer builds it in sections; see rewriter.ARTICLE_SECTIONS.
MIN_ARTICLE_WORDS = env_int("MIN_ARTICLE_WORDS", 420)
MAX_ARTICLE_WORDS = env_int("MAX_ARTICLE_WORDS", 900)
TARGET_ARTICLE_WORDS = env_int("TARGET_ARTICLE_WORDS", 600)

# How much source text to feed the fact extractor per source, in characters.
# Tavily's extract endpoint returns full article text, which is what makes a
# 600-word article possible; the snippet alone is far too thin.
SOURCE_TEXT_BUDGET = env_int("SOURCE_TEXT_BUDGET", 24000)
USE_FULL_TEXT_EXTRACTION = env_bool("USE_FULL_TEXT_EXTRACTION", True)

# --- Images ---------------------------------------------------------------
# Only openly-licensed images are ever downloaded. Never the outlets' own
# photography, which is agency-licensed and cannot lawfully be republished.
FETCH_IMAGES = env_bool("FETCH_IMAGES", True)
IMAGE_MAX_WIDTH = env_int("IMAGE_MAX_WIDTH", 1280)
IMAGE_JPEG_QUALITY = env_int("IMAGE_JPEG_QUALITY", 82)
IMAGE_MAX_BYTES = env_int("IMAGE_MAX_BYTES", 12 * 1024 * 1024)
IMAGE_HTTP_TIMEOUT = env_int("IMAGE_HTTP_TIMEOUT", 25)

# --- Retention ------------------------------------------------------------
# Storage is finite, so stories nobody read are purged after a week along with
# their images. Anything with real readership is kept.
RETENTION_DAYS = env_int("RETENTION_DAYS", 7)
RETENTION_MIN_VIEWS = env_int("RETENTION_MIN_VIEWS", 5)
RETENTION_KEEP_REVIEWED = env_bool("RETENTION_KEEP_REVIEWED", True)

# Publish automatically, or hold every article in the review queue.
AUTO_PUBLISH = env_bool("AUTO_PUBLISH", True)

# Daily scheduler run time, local to TIME_ZONE, used by `manage.py run_scheduler`.
SCHEDULE_HOUR = env_int("SCHEDULE_HOUR", 6)
SCHEDULE_MINUTE = env_int("SCHEDULE_MINUTE", 30)

# Seed queries per category used to discover what is trending today.
#
# These deliberately name *subjects* rather than asking for "top news today".
# Meta queries rank roundups, live blogs and section fronts, because those pages
# are built to rank for that phrasing. Subject queries return the individual
# articles we actually want to write about.
# Emphasis: finance, politics, sports, tech, and device launches (phones/laptops).
NEWS_CATEGORY_QUERIES = {
    "world": [
        "United States White House politics announcement",
        "US Congress bill politics vote",
        "US Iran foreign policy development",
        "election politics campaign news",
    ],
    "india": [
        "Indian government policy politics decision",
        "India Parliament bill politics",
        "India Supreme Court ruling",
    ],
    "business": [
        "Wall Street stock market finance news",
        "Federal Reserve interest rate finance decision",
        "US company quarterly earnings results",
        "US merger acquisition deal announced",
        "banking finance sector news",
        "S&P 500 stock market rally",
    ],
    "technology": [
        "smartphone launch announced",
        "Apple iPhone Samsung Galaxy launch",
        "Google Pixel foldable phone launch",
        "new laptop notebook launch announced",
        "MacBook Windows laptop release",
        "wearable smartwatch device launch",
        "artificial intelligence product release",
        "chip semiconductor company news",
        "cybersecurity data breach United States",
        "mobile phone tablet device launch India",
    ],
    "sports": [
        "NBA game result",
        "NFL football match report",
        "Premier League football match report",
        "cricket match result India",
        "Premier League transfer news",
        "Olympics athletics championship result",
    ],
    "entertainment": [
        "Hollywood movie release announcement",
        "US film box office collection",
    ],
    "rumours": [
        "tech company acquisition rumours sources",
        "football transfer rumours reported sources",
    ],
    "science": [
        "NASA space mission launch",
        "medical research study findings United States",
    ],
}

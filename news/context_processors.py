from django.conf import settings


def site_settings(request):
    return {
        "site_name": settings.SITE_NAME,
        "site_tagline": settings.SITE_TAGLINE,
    }

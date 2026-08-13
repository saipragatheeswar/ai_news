from django.conf import settings


def site_settings(request):
    return {
        "site_name": settings.SITE_NAME,
        "site_tagline": settings.SITE_TAGLINE,
        "site_publisher": settings.SITE_PUBLISHER,
        "contact_email": settings.CONTACT_EMAIL,
        "publisher_country": settings.PUBLISHER_COUNTRY,
        "site_base_url": settings.SITE_BASE_URL,
        "adsense_client": settings.ADSENSE_CLIENT,
        "adsense_slot_infeed": settings.ADSENSE_SLOT_INFEED,
        "adsense_slot_sidebar": settings.ADSENSE_SLOT_SIDEBAR,
        "adsense_slot_article": settings.ADSENSE_SLOT_ARTICLE,
        "adsense_infeed_every": settings.ADSENSE_INFEED_EVERY,
    }

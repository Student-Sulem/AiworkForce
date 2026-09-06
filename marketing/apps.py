from django.apps import AppConfig


class MarketingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'marketing'
    verbose_name = 'AI Workforce'

    def ready(self):
        """Import signal receivers once the app registry is populated.

        The import is done here rather than at module level because signals.py
        imports models, and models cannot be imported while Django is still
        building the app registry.
        """
        from . import signals  # noqa: F401

from django.apps import AppConfig


class AfyaConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "afya"
    verbose_name = "Afya Mfukoni"

    def ready(self):
        from afya import checks  # noqa: F401 - registers the startup checks

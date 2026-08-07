from django.apps import AppConfig


class CmsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "cms"
    verbose_name = "Website Content"

    def ready(self):
        from django.contrib import admin

        from .admin_dashboard import patch_admin_index_with_stats

        patch_admin_index_with_stats(admin.site)

from django.apps import AppConfig


class RmaConfig(AppConfig):
    name = "inward.rma"
    label = "rma"
    verbose_name = "Material returns"

    def ready(self):
        from inward.rma import masterfiles
        masterfiles.setup()

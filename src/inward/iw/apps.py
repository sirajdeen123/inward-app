from django.apps import AppConfig


class IwConfig(AppConfig):
    name = "inward.iw"
    label = "iw"
    verbose_name = "IW vehicles and consignments"

    def ready(self):
        from inward.accounts import approvals
        from inward.iw import services

        approvals.register("IW_VOID", services.apply_void)

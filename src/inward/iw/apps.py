from django.apps import AppConfig


class IwConfig(AppConfig):
    name = "inward.iw"
    label = "iw"
    verbose_name = "IW vehicles and consignments"

    def ready(self):
        from inward.accounts import approvals
        from inward.iw import services, unloading

        approvals.register("IW_VOID", services.apply_void)
        approvals.register("UNLOADING_EXEMPT", unloading.apply_exempt)

from __future__ import annotations

from django import forms
from django.utils import timezone

from inward.core.models import LoadType, Organisation


class IwForm(forms.Form):
    arrival_at = forms.DateTimeField(label="Arrival date-time *", input_formats=["%Y-%m-%dT%H:%M"],
                                     widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"))
    load_type = forms.ModelChoiceField(label="Load type *", queryset=LoadType.objects.filter(active=True),
                                       empty_label="Select load type")
    source_org = forms.CharField(label="Source of load (organisation)", required=False, max_length=140,
                                 widget=forms.TextInput(attrs={"list": "org-list", "placeholder": "type code or name, e.g. 390"}))
    vehicle_no = forms.CharField(label="Vehicle number *", max_length=20,
                                 widget=forms.TextInput(attrs={"autocapitalize": "characters"}))
    driver_name = forms.CharField(label="Driver name", required=False, max_length=80)
    driver_mobile = forms.CharField(label="Driver mobile", required=False, max_length=15,
                                    widget=forms.TextInput(attrs={"inputmode": "tel"}))
    transporter = forms.CharField(label="Transporter", required=False, max_length=160,
                                  widget=forms.TextInput(attrs={"list": "transporter-list"}))
    lr_no = forms.CharField(label="LR number", required=False, max_length=40)
    remarks = forms.CharField(label="Remarks", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    version = forms.IntegerField(required=False, widget=forms.HiddenInput)

    def clean_source_org(self):
        text = self.cleaned_data["source_org"].strip()
        if not text:
            return None
        code = text.split(":", 1)[0].strip()
        org = Organisation.objects.filter(code=code).first() or Organisation.objects.filter(name__iexact=text).first()
        if org is None:
            raise forms.ValidationError("Unknown organisation. Pick one from the list (it fills from SIT imports).")
        return org

    @staticmethod
    def initial_for(iw=None) -> dict:
        if iw is None:
            return {"arrival_at": timezone.localtime().replace(second=0, microsecond=0)}
        return {"arrival_at": timezone.localtime(iw.arrival_at), "load_type": iw.load_type_id,
                "source_org": str(iw.source_org) if iw.source_org else "", "vehicle_no": iw.vehicle_no,
                "driver_name": iw.driver_name, "driver_mobile": iw.driver_mobile,
                "transporter": iw.transporter.name if iw.transporter else "", "lr_no": iw.lr_no,
                "remarks": iw.remarks, "version": iw.version}


class VoidForm(forms.Form):
    reason = forms.CharField(label="Reason for VOID *", widget=forms.Textarea(attrs={"rows": 3}), max_length=500)

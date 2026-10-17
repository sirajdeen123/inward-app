from __future__ import annotations

from datetime import datetime

from django import forms
from django.utils import timezone

from inward.core.models import LoadType, Organisation


HOURS = [(str(h), str(h)) for h in range(1, 13)]
MINUTES = [(f"{m:02d}", f"{m:02d}") for m in range(60)]
AMPM = [("AM", "AM"), ("PM", "PM")]


class ArrivalWidget(forms.MultiWidget):
    """Date + hour (1-12) + minute + AM / PM (UAT feedback 17-Oct-2026: 12-hour clock)."""

    def __init__(self):
        super().__init__([forms.DateInput(attrs={"type": "date", "aria-label": "Arrival date"}, format="%Y-%m-%d"),
                          forms.Select(choices=HOURS, attrs={"aria-label": "Hour", "class": "t-part"}),
                          forms.Select(choices=MINUTES, attrs={"aria-label": "Minute", "class": "t-part"}),
                          forms.Select(choices=AMPM, attrs={"aria-label": "AM or PM", "class": "t-part"})])

    def decompress(self, value):
        if not value:
            return [None, None, None, None]
        v = timezone.localtime(value) if timezone.is_aware(value) else value
        return [v.date(), str(v.hour % 12 or 12), f"{v.minute:02d}", "AM" if v.hour < 12 else "PM"]


class ArrivalField(forms.MultiValueField):
    widget = ArrivalWidget

    def __init__(self, **kwargs):
        super().__init__(fields=[forms.DateField(), forms.ChoiceField(choices=HOURS), forms.ChoiceField(choices=MINUTES),
                                 forms.ChoiceField(choices=AMPM)], require_all_fields=True, **kwargs)

    def compress(self, data_list):
        if not data_list:
            return None
        day, hour, minute, ampm = data_list
        h = int(hour) % 12 + (12 if ampm == "PM" else 0)
        return timezone.make_aware(datetime(day.year, day.month, day.day, h, int(minute)))


class IwForm(forms.Form):
    arrival_at = ArrivalField(label="Arrival date and time *")
    load_type = forms.ModelChoiceField(label="Load type *", queryset=LoadType.objects.filter(active=True),
                                       empty_label="Select load type")
    source_org = forms.CharField(label="Source of load (organisation)", required=False, max_length=140,
                                 widget=forms.TextInput(attrs={"list": "org-list", "placeholder": "type code or name, e.g. 390"}))
    vehicle_no = forms.CharField(label="Vehicle number *", max_length=20,
                                 widget=forms.TextInput(attrs={"autocapitalize": "characters", "class": "caps"}))
    driver_name = forms.CharField(label="Driver name", required=False, max_length=80,
                                  widget=forms.TextInput(attrs={"autocapitalize": "characters", "class": "caps"}))
    driver_mobile = forms.CharField(label="Driver mobile", required=False, max_length=15,
                                    widget=forms.TextInput(attrs={"inputmode": "tel"}))
    transporter = forms.CharField(label="Transporter", required=False, max_length=160,
                                  widget=forms.TextInput(attrs={"list": "transporter-list"}))
    lr_no = forms.CharField(label="LR number", required=False, max_length=40,
                            widget=forms.TextInput(attrs={"class": "caps"}))
    gate_register_no = forms.CharField(label="Security gate register No (needed before the MRA)", required=False,
                                       max_length=30, widget=forms.TextInput(attrs={"class": "caps"}))
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
                "gate_register_no": iw.gate_register_no,
                "remarks": iw.remarks, "version": iw.version}


class VoidForm(forms.Form):
    reason = forms.CharField(label="Reason for VOID *", widget=forms.Textarea(attrs={"rows": 3}), max_length=500)

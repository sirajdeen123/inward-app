"""Company branding on documents, screens and e-mails (SoT 7.4 as amended in v1.6.1).

Header: logo + company name (Depot master) + depot + "Operated by: <CFA> (CFA)".
Depot signature block: "For <COMPANY>" / "Operated by <CFA> - <code> <depot> Depot".
"""
from __future__ import annotations

import base64
from functools import lru_cache

from django.contrib.staticfiles import finders

LOGO = "inward/company_logo.png"


@lru_cache(maxsize=1)
def logo_data_uri() -> str:
    path = finders.find(LOGO)
    if not path:
        return ""
    with open(path, "rb") as fh:
        return "data:image/png;base64," + base64.b64encode(fh.read()).decode()


def seal_lines(company: str, cfa: str, depot_code: str, depot_name: str) -> dict:
    return {"for": f"For {company}" if company else "",
            "operated": f"Operated by {cfa} - {depot_code} {depot_name} Depot" if cfa else ""}

"""Default reference data (SoT 12, 12.1, 23). The CFA Manager changes it in the application afterwards."""
from __future__ import annotations

ROLES = {
    "DEPOT_CLERK": "Depot Clerk",
    "SYSTEM_SUPERVISOR": "System Supervisor",
    "CFA_MANAGER": "CFA Manager",
    "ADMINISTRATOR": "Administrator",
    "VIEWER": "Viewer (Depot Manager / Admin Officer)",
}
STAFF_ROLES = {"CFA_MANAGER", "ADMINISTRATOR"}  # may open the Django admin area (SoT 14.5)

PERMISSIONS = {
    "IW_EDIT": "Create / edit IW before MRA",
    "MATERIAL_LINK": "Link material; assign outbound rows",
    "PHYSICAL_SED_QTY": "Physical verification and SED quantities",
    "SED_VALUE": "SED financial values",
    "UNLOADING_RECORD": "Unloading charge / payment",
    "UNLOADING_EXEMPT": "Exempt / Nil unloading",
    "MRA_ISSUE": "Issue MRA, trigger communications",
    "EMAIL_VERIFY": "Verify clerk-entered sender / organisation e-mails",
    "SED_EVIDENCE_ACCEPT": "Accept manually uploaded SED approval evidence",
    "RECEIPT_ENTRY": "Enter Stock / PO / Excess Receipt No",
    "SED_TRX_LSCRR": "Enter SED TRX No; record the LSCCR No",
    "IMPORT_RECONCILE": "Import SIT / IRR; run reconciliation",
    "IW_CLOSE": "Close IW",
    "IW_REOPEN": "Reopen closed IW",
    "IW_VOID": "VOID IW (before MRA only)",
    "MAIL_PENDING_RELEASE": "Mail-Pending Release",
    "EXCEPTION_RESOLVE": "Resolve critical exceptions",
    "MASTER_CHANGE": "Propose / approve master changes",
    "SKU_PACKAGING_EDIT": "Upload / add / edit SKU packaging",
    "PACKAGING_REVIEW": "Review drastic SKU packaging changes",
    "USER_ADMIN": "Manage users, roles, permissions",
    "TWO_FACTOR_RESET": "Reset a user's two-factor sign-in",
    "SYSTEM_ADMIN": "System configuration, backup, health",
    "SEARCH_REPORTS": "Search, audit view, reports",
}

G, R, A, V = "GRANT", "REQUEST", "APPROVE", "VIEW"
# permission -> {role: mode}; absent = no access
MATRIX: dict[str, dict[str, str]] = {
    "IW_EDIT": {"DEPOT_CLERK": G, "SYSTEM_SUPERVISOR": G, "CFA_MANAGER": G},
    "MATERIAL_LINK": {"DEPOT_CLERK": G, "SYSTEM_SUPERVISOR": G, "CFA_MANAGER": G},
    "PHYSICAL_SED_QTY": {"DEPOT_CLERK": G, "SYSTEM_SUPERVISOR": G, "CFA_MANAGER": G},
    "SED_VALUE": {"DEPOT_CLERK": G, "CFA_MANAGER": G},
    "UNLOADING_RECORD": {"DEPOT_CLERK": G, "CFA_MANAGER": G},
    "UNLOADING_EXEMPT": {"DEPOT_CLERK": R, "CFA_MANAGER": A},
    "MRA_ISSUE": {"DEPOT_CLERK": G, "CFA_MANAGER": G},
    "EMAIL_VERIFY": {"CFA_MANAGER": G},
    "SED_EVIDENCE_ACCEPT": {"CFA_MANAGER": G},
    "RECEIPT_ENTRY": {"DEPOT_CLERK": G, "SYSTEM_SUPERVISOR": G, "CFA_MANAGER": G},
    "SED_TRX_LSCRR": {"DEPOT_CLERK": G, "SYSTEM_SUPERVISOR": G, "CFA_MANAGER": G},
    "IMPORT_RECONCILE": {"DEPOT_CLERK": G, "SYSTEM_SUPERVISOR": G, "CFA_MANAGER": G},
    "IW_CLOSE": {"DEPOT_CLERK": G, "CFA_MANAGER": G},
    "IW_REOPEN": {"DEPOT_CLERK": R, "SYSTEM_SUPERVISOR": R, "CFA_MANAGER": A},
    "IW_VOID": {"DEPOT_CLERK": R, "SYSTEM_SUPERVISOR": R, "CFA_MANAGER": A},
    "MAIL_PENDING_RELEASE": {"CFA_MANAGER": G},
    "EXCEPTION_RESOLVE": {"SYSTEM_SUPERVISOR": R, "CFA_MANAGER": A},
    "MASTER_CHANGE": {"DEPOT_CLERK": R, "SYSTEM_SUPERVISOR": R, "CFA_MANAGER": A},
    "SKU_PACKAGING_EDIT": {"DEPOT_CLERK": G, "SYSTEM_SUPERVISOR": G, "CFA_MANAGER": G},
    "PACKAGING_REVIEW": {"CFA_MANAGER": G},
    "USER_ADMIN": {"CFA_MANAGER": G, "ADMINISTRATOR": G},
    "TWO_FACTOR_RESET": {"CFA_MANAGER": R, "ADMINISTRATOR": A},
    "SYSTEM_ADMIN": {"CFA_MANAGER": V, "ADMINISTRATOR": G},
    "SEARCH_REPORTS": {"DEPOT_CLERK": G, "SYSTEM_SUPERVISOR": G, "CFA_MANAGER": G, "ADMINISTRATOR": G, "VIEWER": G},
}

LOAD_TYPES = [
    # code, name, inbound SIT, outbound, direct factory, vendor, non-stock
    ("INTER_DEPOT", "Inter Depot Receipt", True, False, False, False, False),
    ("INTER_DEPOT_INBOUND_TS", "Inter Depot + Inbound Transhipment", True, False, False, False, False),
    ("INBOUND_TS", "Inbound Transhipment", True, False, False, False, False),
    ("RDC", "RDC Receipt", True, False, False, False, False),
    ("RDC_OUTBOUND_TS", "RDC + Outbound Transhipment", True, True, False, False, False),
    ("DIRECT_FACTORY", "Direct Factory Receipt", True, False, True, False, False),
    ("DIRECT_FACTORY_OUTBOUND_TS", "Direct Factory + Outbound Transhipment", True, True, True, False, False),
    ("VENDOR", "Vendors", False, False, False, True, False),
    ("OTHERS_NON_STOCK", "Others (Non-Stock)", False, False, False, False, True),
]

# checklist groups in unloading order (cartons first); confirmed 15-Oct-2026
PACKAGE_TYPES = [("CARTON", "Cartons / Boxes", 10), ("DRUM", "Drums", 20), ("BARREL", "Barrels", 25),
                 ("PAIL", "Pails", 28), ("BAG", "Bags", 30), ("PACKET", "Packets / Pieces / Bundles", 40),
                 ("TIN", "Tins", 50), ("LOOSE", "Loose", 60), ("MACHINE", "Machines", 70),
                 ("EMPTY_PACKING", "Fresh empty packing", 80), ("SHOTS", "Colorant shots", 85), ("OTHER", "Other", 90)]

# company list "Pack Type" -> checklist group
PACK_TYPE_MAP = {"CARTON BOX": "CARTON", "CARTON": "CARTON", "BOX": "CARTON", "DRUM": "DRUM", "BARREL": "BARREL",
                 "PAIL": "PAIL", "BAG": "BAG", "PACKET": "PACKET", "PCS": "PACKET", "BUNDLE": "PACKET",
                 "TIN": "TIN", "EMPTY CONT": "EMPTY_PACKING", "SHOTS": "SHOTS", "MACHINE": "MACHINE"}

# Business line -> warehouse for the split unloading checklist (SoT 7.1); confirmed 14-Oct-2026
BUSINESS_LINE_WAREHOUSE = {
    "RETAIL": "RETAIL",
    "PROTECTON": "INDUSTRIAL",
    "POWDER": "INDUSTRIAL",
    "GI": "INDUSTRIAL",
    "AUTOMOTIVE": "INDUSTRIAL",
}

FEATURE_FLAGS = {
    "colour_bank_erp_tracked": (False, "Colour Bank tracks go through ERP/IRR reconciliation (reserved, OFF)"),
    "claim_approval_threshold_rs": (None, "SED claim value above which CFA Manager approval is needed (null = off)"),
    "mail_pending_release_after_hours": (2, "Hours a mandatory mail must be pending before Mail-Pending Release"),
    "sed_approval_reminder_hours": (24, "Hours before the Depot Manager is reminded about an SED approval"),
    "split_vehicle_unallocated_days": (7, "Days before 'Ship balance not received' is raised"),
    "outbound_row_reuse_days": (60, "Window for warning about re-use of an identical Despatch row"),
    "measure_tolerance": ("0.001", "LTR/KG tolerance per SKU in reconciliation"),
    "cloud_only_after_months": (12, "Closed IW documents kept on OneDrive only after this many months"),
    "daily_report_time": ("19:00", "Time (IST) after which the daily escalation and unloading reports are sent"),
    "packaging_bulk_change_limit": (50, "An upload changing more existing SKUs than this is flagged as a whole"),
}

# purpose -> [(role label, TO/CC)]
NOTIFICATION_PURPOSES = {
    "SED_IMMEDIATE": [("Depot Manager", "TO"), ("Admin Officer", "TO"), ("CFA Manager", "TO")],
    "SED_APPROVAL": [("Depot Manager", "TO"), ("Admin Officer", "CC")],
    "DAILY_ESCALATION": [("Admin Officer", "TO"), ("CFA Manager", "TO"), ("Clerk", "TO")],
    "MONTHLY_MIS": [("Admin Officer", "TO"), ("CFA Manager", "TO"), ("Clerk", "TO")],
    "SED_REPORT": [("Admin Officer", "TO"), ("CFA Manager", "TO"), ("Clerk", "TO")],
    "IRR_RECON_REPORT": [("Admin Officer", "TO"), ("CFA Manager", "TO"), ("Clerk", "TO")],
    "DAILY_UNLOADING": [("CFA Manager", "TO")],
}

DEFAULT_RATES = {"UNLOADING_PER_TON": "300.00", "DAMAGE_CARTON": "50.00", "DAMAGE_DRUM": "150.00"}

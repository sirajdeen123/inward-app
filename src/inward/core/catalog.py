"""Default reference data (SoT 12, 12.1, 23). The CFA Manager changes it in the application afterwards."""
from __future__ import annotations

ROLES = {
    "DEPOT_CLERK": "Depot Clerk",
    "SYSTEM_SUPERVISOR": "System Supervisor",
    "CFA_MANAGER": "CFA Manager",
    "ADMINISTRATOR": "Administrator",
    "VIEWER": "Viewer (Depot Manager / Admin Officer)",
    "CASHIER": "Cashier (cash register only)",
}
STAFF_ROLES = {"CFA_MANAGER", "ADMINISTRATOR"}  # may open the Django admin area (SoT 14.5)

PERMISSIONS = {
    "IRR_RECON_MARK": "IRR reconciliation engine: run it, explain exceptions",
    "RMA_WORK": "Material returns: GRN, portal / CN / master uploads, links, IRR TRX",
    "RMA_CLOSE": "Material returns: close a return after the godown check (or a rejected one)",
    "RMA_MASTER": "Material returns: portal approver e-mails, Sales Officer e-mails",
    "RMA_URGENT_MAIL": "Material returns: send an urgent approval reminder to chosen approvers",
    "DAMAGE_REPORT": "Internal damage: list damaged material with photos, send for approval",
    "DAMAGE_TRX": "Internal damage: enter the DMG transfer TRX No, reconcile, decide reason codes",
    "CASH_REGISTER": "Cash register: receive cash, expenses, handovers, statement",
    "CASH_MANAGE": "Cash register: opening balance, confirm / cancel any entry (manager)",
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
    "SCAN_UPLOAD": "Upload the signed MRA / LR scans (final documents)",
    "IW_REOPEN": "Reopen closed IW",
    "IW_VOID": "VOID IW (before MRA only)",
    "IW_CORRECT": "Correct an IW after its MRA, before any ERP entry (reopened; the MRA is re-issued)",
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
    "SCAN_UPLOAD": {"DEPOT_CLERK": G, "SYSTEM_SUPERVISOR": G, "CFA_MANAGER": G},   # 22-Oct-2026: supervisors too
    "IW_REOPEN": {"DEPOT_CLERK": R, "SYSTEM_SUPERVISOR": R, "CFA_MANAGER": A},
    "IW_VOID": {"DEPOT_CLERK": R, "SYSTEM_SUPERVISOR": R, "CFA_MANAGER": A},
    "IW_CORRECT": {"DEPOT_CLERK": R, "SYSTEM_SUPERVISOR": R, "CFA_MANAGER": A},
    "MAIL_PENDING_RELEASE": {"CFA_MANAGER": G},
    "EXCEPTION_RESOLVE": {"SYSTEM_SUPERVISOR": R, "CFA_MANAGER": A},
    "MASTER_CHANGE": {"DEPOT_CLERK": R, "SYSTEM_SUPERVISOR": R, "CFA_MANAGER": A},
    "SKU_PACKAGING_EDIT": {"DEPOT_CLERK": G, "SYSTEM_SUPERVISOR": G, "CFA_MANAGER": G, "ADMINISTRATOR": G,
                           "VIEWER": G, "CASHIER": G},   # 23-Oct-2026: all users; the CFA Manager is mailed
    "PACKAGING_REVIEW": {"CFA_MANAGER": G},
    "USER_ADMIN": {"CFA_MANAGER": G, "ADMINISTRATOR": G},
    "TWO_FACTOR_RESET": {"CFA_MANAGER": R, "ADMINISTRATOR": A},
    "SYSTEM_ADMIN": {"CFA_MANAGER": V, "ADMINISTRATOR": G},
    "SEARCH_REPORTS": {"DEPOT_CLERK": G, "SYSTEM_SUPERVISOR": G, "CFA_MANAGER": G, "ADMINISTRATOR": G, "VIEWER": G},
    "CASH_REGISTER": {"CASHIER": G, "CFA_MANAGER": G},
    "DAMAGE_REPORT": {"DEPOT_CLERK": G, "SYSTEM_SUPERVISOR": G, "CFA_MANAGER": G},
    "DAMAGE_TRX": {"SYSTEM_SUPERVISOR": G, "CFA_MANAGER": G},
    "IRR_RECON_MARK": {"SYSTEM_SUPERVISOR": G, "CFA_MANAGER": G},
    "CASH_MANAGE": {"CFA_MANAGER": G},
    "RMA_WORK": {"DEPOT_CLERK": G, "SYSTEM_SUPERVISOR": G, "CFA_MANAGER": G},
    "RMA_CLOSE": {"SYSTEM_SUPERVISOR": G, "CFA_MANAGER": G},
    "RMA_MASTER": {"SYSTEM_SUPERVISOR": G, "CFA_MANAGER": G},
    "RMA_URGENT_MAIL": {"DEPOT_CLERK": G, "SYSTEM_SUPERVISOR": G, "CFA_MANAGER": G},
}

LOAD_TYPES = [
    # code, name, inbound SIT, outbound, direct factory, vendor, non-stock
    ("INTER_DEPOT", "Inter Depot Receipt", True, False, False, False, False),
    ("INTER_DEPOT_INBOUND_TS", "Inter Depot + Inbound Transhipment", True, False, False, False, False),
    ("INBOUND_TS", "Inbound Transhipment", True, False, False, False, False),
    ("OUTBOUND_TS", "Outbound Transhipment only", False, True, False, False, False),   # UAT 17-Oct-2026
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
    "rma_go_live": ("2026-10-22", "Material returns go-live: requests already processed / rejected before it are "
                                  "kept for reference only (not chased)"),
    "rma_cn_mail_subject": ("XXGST BPIL RMA CN INV Report", "Material returns: part of the subject of the daily RMA "
                                                             "CN INV Report mail (fetched from Gmail)"),
    "rma_cn_fetch_time": ("08:30", "Material returns: look for today's CN report mail from this time"),
    "rma_cn_fetch_deadline": ("11:00", "Material returns: no CN report mail by this time = recorded as missing"),
    "rma_approval_time": ("09:30", "Material returns: approval-pending digest to each portal approver after this time"),
    "rma_daily_time": ("09:30", "Material returns: daily report to the Admin Officer after this time"),
    "rma_daily_cc_rcm": (True, "Material returns: copy the RCM on the daily report (switch on Notification e-mails)"),
    "rma_amber_days": (7, "Material returns: days pending before a request turns amber"),
    "rma_red_days": (15, "Material returns: days pending before a request turns red"),
    "colour_bank_erp_tracked": (False, "Colour Bank tracks go through ERP/IRR reconciliation (reserved, OFF)"),
    "claim_approval_threshold_rs": (None, "SED claim value above which CFA Manager approval is needed (null = off)"),
    "mail_pending_release_after_hours": (2, "Hours a mandatory mail must be pending before Mail-Pending Release"),
    "sed_approval_reminder_hours": (24, "Hours before the Depot Manager is reminded about an SED approval"),
    "split_vehicle_unallocated_days": (7, "Days before 'Ship balance not received' is raised"),
    "outbound_row_reuse_days": (60, "Window for warning about re-use of an identical Despatch row"),
    "measure_tolerance": ("0.001", "LTR/KG tolerance per SKU in reconciliation"),
    "cloud_only_after_months": (12, "Closed IW documents kept on OneDrive only after this many months"),
    "daily_unloading_time": ("09:00", "Time (IST) after which yesterday's unloading collection is mailed"),
    "daily_status_time": ("09:30", "Time (IST) after which the daily status of incomplete IWs is mailed"),
    "damage_reason_codes": (["63", "68"], "Oracle reason codes accepted for internal damage transfers to DMG; "
                                          "any other code is flagged for the System Supervisor"),
    "irr_mail_subject": ("CBPPROD2 : BPIL Issue - Receipt Register", "Daily IRR mail: text contained in its subject (partial match)"),
    "irr_fetch_time": ("08:30", "Daily IRR mail: from this time (IST) the worker looks for today's IRR every 10 minutes"),
    "irr_fetch_deadline": ("11:00", "Daily IRR mail: if none has come by this time, the summary says so"),
    "irr_recon_damage_from": ("2026-10-18", "IRR engine: transfers to DMG from this date need an approved internal "
                                             "damage report (earlier ones are history)"),
    "damage_approval_reminder_hours": (24, "Hours after the internal damage approval mail before one reminder"),
    "packaging_bulk_change_limit": (50, "An upload changing more existing SKUs than this is flagged as a whole"),
}

# purpose -> [(role label, TO/CC)]
NOTIFICATION_PURPOSES = {
    "SED_IMMEDIATE": [("Depot Manager", "TO"), ("Admin Officer", "TO"), ("CFA Manager", "TO")],
    "SED_APPROVAL": [("Depot Manager", "TO"), ("Admin Officer", "CC")],
    "DAILY_ESCALATION": [("Admin Officer", "TO"), ("CFA Manager", "TO")],
    "MONTHLY_MIS": [("Admin Officer", "TO"), ("CFA Manager", "TO"), ("Clerk", "TO")],
    "SED_REPORT": [("Admin Officer", "TO"), ("CFA Manager", "TO"), ("Clerk", "TO")],
    "IRR_RECON_REPORT": [("Admin Officer", "TO"), ("CFA Manager", "TO"), ("Clerk", "TO")],
    "DAILY_UNLOADING": [("CFA Manager", "TO")],
    "DAMAGE_APPROVAL": [("Depot Manager", "TO"), ("Admin Officer", "TO"), ("CFA Manager", "CC")],
    "IRR_EXCEPTIONS": [("System Supervisor", "TO"), ("CFA Manager", "TO"), ("Admin Officer", "TO")],
    # material returns (21-Oct-2026): the approvers themselves are mapped on the Approvers screen
    "RMA_APPROVAL": [("Admin Officer", "CC"), ("Godown", "CC")],
    "PACKAGING_CHANGE": [("CFA Manager", "TO")],
    "RMA_DAILY": [("Admin Officer", "TO"), ("CFA Manager", "CC"), ("RCM", "CC")],
}

# internal damage reasons the app starts with (code, name, photo required, remarks required, order)
DAMAGE_CATEGORIES = [("HANDLING", "Handling damage (labour)", True, False, 10), ("EXPIRY", "Expiry", False, False, 20),
                     ("LEAKAGE", "Lid open / leakage", True, False, 30), ("OTHER", "Other", True, True, 90)]

DEFAULT_RATES = {"UNLOADING_PER_TON": "300.00", "DAMAGE_CARTON": "50.00", "DAMAGE_DRUM": "150.00"}

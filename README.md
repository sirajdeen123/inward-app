# Stock Inward Process application

Coimbatore Depot 035 · operated by RAINBOW LOGISTICS (CFA).
Controlling specification: `Stock_Inward_Process_Source_of_Truth_v1.5.docx` (locked). Earlier versions are superseded.

## Stack
Django 5.2 LTS + HTMX · PostgreSQL · Gmail (SMTP + IMAP) · two-factor sign-in for every user (django-otp) ·
account lockout (django-axes) · Argon2 passwords · Hostinger KVM 2 VPS (Ubuntu 24.04, Docker, Nginx, Gunicorn).

## Layout

| Path | What |
|---|---|
| `src/inward/domain/` | Pure business rules: normalisation, UOM families, Floor SKU, FY/IW number, SIT change detection, IRR merge |
| `src/inward/importers/` | SIT, IRR, Despatch Detail (outbound), Vendor portal importers |
| `src/inward/accounts/` | Users, roles/permission matrix, maker-checker approvals, two-factor sign-in, trusted devices |
| `src/inward/core/` | Masters, audit trail (append-only), document store, IW numbering, imports, exceptions, seed data |
| `src/inward/mail/` | Mail transport interface + Gmail implementation |
| `src/inward/web/` | Screens (templates, views), security headers |
| `src/config/` | Django settings and URLs |
| `tests/` | Unit, sample-file and database tests |
| `var/archive/` | Superseded Phase 0 code (Streamlit / SQLAlchemy / Outlook), kept for reference |

## Local setup (Windows)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\scripts\setup_database.ps1                      # once: creates the databases and .env
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py seed_reference_data --go-live 2026-10-19
.\.venv\Scripts\python.exe manage.py create_inward_user --username cfa.manager --full-name "CFA Manager" --role CFA_MANAGER
.\.venv\Scripts\python.exe manage.py runserver
```

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest --basetemp=var\pytest
```

## Gmail spike

```powershell
.\.venv\Scripts\python.exe manage.py mail_spike check
.\.venv\Scripts\python.exe manage.py mail_spike find-sit --parse
.\.venv\Scripts\python.exe manage.py mail_spike send-test --to your.address@example.com
```

import getpass

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from inward.accounts.services import create_user
from inward.core.catalog import ROLES


class Command(BaseCommand):
    help = "Create a user with a role (server operator use, e.g. the first CFA Manager). Prompts for the password."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument("--full-name", required=True)
        parser.add_argument("--email", default="")
        parser.add_argument("--role", required=True, choices=sorted(ROLES))
        parser.add_argument("--break-glass", action="store_true")

    def handle(self, *args, **o):
        password = getpass.getpass(f"Password for {o['username']} (min 12 characters): ")
        if password != getpass.getpass("Repeat password: "):
            raise CommandError("Passwords differ")
        try:
            create_user(None, username=o["username"], full_name=o["full_name"], email=o["email"],
                        password=password, role_code=o["role"], break_glass=o["break_glass"])
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc
        self.stdout.write(self.style.SUCCESS(
            f"User {o['username']} created with role {o['role']}. Two-factor set-up starts at first sign-in."))

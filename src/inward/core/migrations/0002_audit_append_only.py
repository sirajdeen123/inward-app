"""The audit trail is append-only at database level (SoT 1, 19).

UPDATE and DELETE are always rejected. TRUNCATE is rejected too, except in the disposable automated-test
database (name ending '_test' or starting 'test_'), which the test framework empties between tests.
"""
from django.db import migrations

FORWARD = r"""
CREATE FUNCTION audit_event_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'TRUNCATE' AND (current_database() LIKE '%\_test' OR current_database() LIKE 'test\_%') THEN
        RETURN NULL;
    END IF;
    RAISE EXCEPTION 'audit_event is append-only';
END $$;
CREATE TRIGGER audit_event_no_update BEFORE UPDATE OR DELETE ON audit_event
    FOR EACH ROW EXECUTE FUNCTION audit_event_append_only();
CREATE TRIGGER audit_event_no_truncate BEFORE TRUNCATE ON audit_event
    FOR EACH STATEMENT EXECUTE FUNCTION audit_event_append_only();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS audit_event_no_truncate ON audit_event;
DROP TRIGGER IF EXISTS audit_event_no_update ON audit_event;
DROP FUNCTION IF EXISTS audit_event_append_only();
"""


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]

"""Per-organisation signing keys for evidence packs.

Revision ID: 0003
Revises: 0002
"""
import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

APP_ROLE = os.environ.get("MA_APP_DB_ROLE", "modelassure_app")
CUR_ORG = "NULLIF(current_setting('app.current_org', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "signing_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("algorithm", sa.String(20), nullable=False, server_default="Ed25519"),
        sa.Column("public_key_pem", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False, unique=True),
        sa.Column("secret_ref", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE"),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
    )
    # At most one active key per organisation (also serialises concurrent first use).
    op.execute("CREATE UNIQUE INDEX signing_keys_one_active ON signing_keys (organization_id) WHERE status = 'ACTIVE'")
    op.execute("ALTER TABLE signing_keys ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE signing_keys FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON signing_keys USING (organization_id = {CUR_ORG}) WITH CHECK (organization_id = {CUR_ORG})")
    # Key material never changes; only status/retired_at may (retirement). Keys are never deleted.
    op.execute("""
        CREATE OR REPLACE FUNCTION ma_signing_key_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'signing keys cannot be deleted' USING ERRCODE = 'insufficient_privilege';
          END IF;
          IF NEW.public_key_pem IS DISTINCT FROM OLD.public_key_pem OR NEW.fingerprint IS DISTINCT FROM OLD.fingerprint
             OR NEW.secret_ref IS DISTINCT FROM OLD.secret_ref OR NEW.organization_id IS DISTINCT FROM OLD.organization_id
             OR (OLD.status = 'RETIRED' AND NEW.status <> 'RETIRED') THEN
            RAISE EXCEPTION 'signing key material is immutable' USING ERRCODE = 'insufficient_privilege';
          END IF;
          RETURN NEW;
        END $$;
    """)
    op.execute("CREATE TRIGGER signing_keys_guard BEFORE UPDATE OR DELETE ON signing_keys FOR EACH ROW EXECUTE FUNCTION ma_signing_key_guard()")
    op.execute(f"""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            GRANT SELECT, INSERT, UPDATE ON signing_keys TO {APP_ROLE};
          END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS signing_keys_guard ON signing_keys")
    op.execute("DROP FUNCTION IF EXISTS ma_signing_key_guard()")
    op.drop_table("signing_keys")

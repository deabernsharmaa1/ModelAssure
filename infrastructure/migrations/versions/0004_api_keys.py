"""Pipeline API keys for the CI/CD release gate.

The table has row-level security for the application role. Authentication must find a key
before the organisation is known, so a SECURITY DEFINER function returns the single row for a
key id (and nothing else). RLS is enabled but not forced, so that function, owned by the
migration role, can read it; the application role is still restricted by the policy.

Revision ID: 0004
Revises: 0003
"""
import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

APP_ROLE = os.environ.get("MA_APP_DB_ROLE", "modelassure_app")
CUR_ORG = "NULLIF(current_setting('app.current_org', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("key_id", sa.String(16), nullable=False, unique=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column("service_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
    )
    op.execute("ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON api_keys USING (organization_id = {CUR_ORG}) WITH CHECK (organization_id = {CUR_ORG})")
    op.execute("""
        CREATE OR REPLACE FUNCTION ma_lookup_api_key(p_key_id text)
        RETURNS TABLE (id uuid, organization_id uuid, secret_hash text, service_user_id uuid, project_id uuid,
                       expires_at timestamptz, revoked_at timestamptz)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
          SELECT k.id, k.organization_id, k.secret_hash, k.service_user_id, k.project_id, k.expires_at, k.revoked_at
          FROM api_keys k WHERE k.key_id = p_key_id
        $$;
    """)
    op.execute("REVOKE ALL ON FUNCTION ma_lookup_api_key(text) FROM PUBLIC")
    # Identity fields never change; a revoked key cannot be revived; keys are never deleted.
    op.execute("""
        CREATE OR REPLACE FUNCTION ma_api_key_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'API keys cannot be deleted; revoke them' USING ERRCODE = 'insufficient_privilege';
          END IF;
          IF NEW.key_id IS DISTINCT FROM OLD.key_id OR NEW.secret_hash IS DISTINCT FROM OLD.secret_hash
             OR NEW.organization_id IS DISTINCT FROM OLD.organization_id OR NEW.service_user_id IS DISTINCT FROM OLD.service_user_id
             OR NEW.project_id IS DISTINCT FROM OLD.project_id OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
             OR NEW.created_by IS DISTINCT FROM OLD.created_by
             OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at) THEN
            RAISE EXCEPTION 'API key identity is immutable' USING ERRCODE = 'insufficient_privilege';
          END IF;
          RETURN NEW;
        END $$;
    """)
    op.execute("CREATE TRIGGER api_keys_guard BEFORE UPDATE OR DELETE ON api_keys FOR EACH ROW EXECUTE FUNCTION ma_api_key_guard()")
    op.execute(f"""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            GRANT SELECT, INSERT, UPDATE ON api_keys TO {APP_ROLE};
            GRANT EXECUTE ON FUNCTION ma_lookup_api_key(text) TO {APP_ROLE};
          END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS api_keys_guard ON api_keys")
    op.execute("DROP FUNCTION IF EXISTS ma_api_key_guard()")
    op.execute("DROP FUNCTION IF EXISTS ma_lookup_api_key(text)")
    op.drop_table("api_keys")

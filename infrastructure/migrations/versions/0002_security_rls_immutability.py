"""Tenant isolation (row-level security), evidence immutability and least-privilege grants.

* Every organisation-owned table: RLS policy `organization_id = app.current_org`.
  The API/worker connect as a NON-owner role, so these policies always apply to them.
* Evidence tables are append-only: UPDATE/DELETE raise. Audit events are additionally
  REVOKEd from the application role.
* Approved policy sets and registered system versions cannot be modified; run configuration
  references cannot change once a run is created.

Revision ID: 0002
Revises: 0001
"""
import os

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

APP_ROLE = os.environ.get("MA_APP_DB_ROLE", "modelassure_app")

TENANT_TABLES = [
    "systems", "system_versions", "projects", "connections", "datasets", "policy_sets", "policies",
    "test_suites", "test_cases", "test_runs", "test_executions", "findings", "finding_executions",
    "reviews", "artifacts", "audit_events", "idempotency_keys",
]
APPEND_ONLY = ["system_versions", "test_suites", "test_cases", "test_executions", "reviews", "artifacts", "audit_events"]

CUR_ORG = "NULLIF(current_setting('app.current_org', true), '')::uuid"
CUR_USER = "NULLIF(current_setting('app.current_user', true), '')::uuid"


def upgrade() -> None:
    for t in TENANT_TABLES:
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {t} USING (organization_id = {CUR_ORG}) WITH CHECK (organization_id = {CUR_ORG})")

    # Memberships: visible inside the current org, and a user can always see their own (login).
    op.execute("ALTER TABLE memberships ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE memberships FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY membership_visibility ON memberships USING (organization_id = {CUR_ORG} OR user_id = {CUR_USER}) "
               f"WITH CHECK (organization_id = {CUR_ORG})")

    # Organizations: visible if it is the current org or the current user is a member.
    op.execute("ALTER TABLE organizations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE organizations FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY org_visibility ON organizations USING (id = {CUR_ORG} OR EXISTS "
               f"(SELECT 1 FROM memberships m WHERE m.organization_id = organizations.id AND m.user_id = {CUR_USER})) "
               f"WITH CHECK (id = {CUR_ORG})")

    # Append-only evidence
    op.execute("""
        CREATE OR REPLACE FUNCTION ma_forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'table % is append-only (evidence integrity)', TG_TABLE_NAME
            USING ERRCODE = 'insufficient_privilege';
        END $$;
    """)
    for t in APPEND_ONLY:
        op.execute(f"CREATE TRIGGER {t}_append_only BEFORE UPDATE OR DELETE ON {t} FOR EACH ROW EXECUTE FUNCTION ma_forbid_change()")

    # Approved policy sets are immutable (a change must be a new version).
    op.execute("""
        CREATE OR REPLACE FUNCTION ma_policy_set_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            IF OLD.approval_status = 'APPROVED' THEN
              RAISE EXCEPTION 'approved policy set % cannot be deleted', OLD.id USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN OLD;
          END IF;
          IF OLD.approval_status = 'APPROVED' THEN
            RAISE EXCEPTION 'approved policy set % is immutable; create a new version', OLD.id USING ERRCODE = 'insufficient_privilege';
          END IF;
          IF OLD.approval_status = 'PENDING_REVIEW' AND (NEW.content IS DISTINCT FROM OLD.content) THEN
            RAISE EXCEPTION 'policy set % is under review and cannot be edited', OLD.id USING ERRCODE = 'insufficient_privilege';
          END IF;
          RETURN NEW;
        END $$;
    """)
    op.execute("CREATE TRIGGER policy_sets_guard BEFORE UPDATE OR DELETE ON policy_sets FOR EACH ROW EXECUTE FUNCTION ma_policy_set_guard()")
    op.execute("""
        CREATE OR REPLACE FUNCTION ma_policy_rows_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE st text;
        BEGIN
          SELECT approval_status INTO st FROM policy_sets WHERE id = COALESCE(OLD.policy_set_id, NEW.policy_set_id);
          IF st IN ('APPROVED', 'PENDING_REVIEW') THEN
            RAISE EXCEPTION 'policies of a % policy set cannot be changed', st USING ERRCODE = 'insufficient_privilege';
          END IF;
          IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
          RETURN NEW;
        END $$;
    """)
    op.execute("CREATE TRIGGER policies_guard BEFORE UPDATE OR DELETE ON policies FOR EACH ROW EXECUTE FUNCTION ma_policy_rows_guard()")

    # Runs: configuration references and the locked snapshot can never change.
    op.execute("""
        CREATE OR REPLACE FUNCTION ma_run_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'test runs cannot be deleted' USING ERRCODE = 'insufficient_privilege';
          END IF;
          IF NEW.config_snapshot IS DISTINCT FROM OLD.config_snapshot
             OR NEW.snapshot_hash IS DISTINCT FROM OLD.snapshot_hash
             OR NEW.suite_id IS DISTINCT FROM OLD.suite_id
             OR NEW.policy_set_id IS DISTINCT FROM OLD.policy_set_id
             OR NEW.baseline_policy_set_id IS DISTINCT FROM OLD.baseline_policy_set_id
             OR NEW.baseline_version_id IS DISTINCT FROM OLD.baseline_version_id
             OR NEW.candidate_version_id IS DISTINCT FROM OLD.candidate_version_id
             OR NEW.organization_id IS DISTINCT FROM OLD.organization_id THEN
            RAISE EXCEPTION 'run configuration is locked' USING ERRCODE = 'insufficient_privilege';
          END IF;
          RETURN NEW;
        END $$;
    """)
    op.execute("CREATE TRIGGER test_runs_guard BEFORE UPDATE OR DELETE ON test_runs FOR EACH ROW EXECUTE FUNCTION ma_run_guard()")

    # Least privilege for the application role
    op.execute(f"""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            GRANT USAGE ON SCHEMA public TO {APP_ROLE};
            GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE};
            REVOKE UPDATE, DELETE ON audit_events FROM {APP_ROLE};
            REVOKE INSERT, UPDATE, DELETE ON organizations FROM {APP_ROLE};
            REVOKE ALL ON alembic_version FROM {APP_ROLE};
          END IF;
        END $$;
    """)


def downgrade() -> None:
    for t in ["test_runs"]:
        op.execute(f"DROP TRIGGER IF EXISTS {t}_guard ON {t}")
    op.execute("DROP TRIGGER IF EXISTS policies_guard ON policies")
    op.execute("DROP TRIGGER IF EXISTS policy_sets_guard ON policy_sets")
    for t in APPEND_ONLY:
        op.execute(f"DROP TRIGGER IF EXISTS {t}_append_only ON {t}")
    for t in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {t}")
        op.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS membership_visibility ON memberships")
    op.execute("ALTER TABLE memberships DISABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS org_visibility ON organizations")
    op.execute("ALTER TABLE organizations DISABLE ROW LEVEL SECURITY")
    for f in ["ma_forbid_change", "ma_policy_set_guard", "ma_policy_rows_guard", "ma_run_guard"]:
        op.execute(f"DROP FUNCTION IF EXISTS {f}()")

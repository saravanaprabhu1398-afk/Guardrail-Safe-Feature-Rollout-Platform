"""Create the production operational control plane.

Revision ID: 20260818_0001
Revises:
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260818_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "experiments",
        sa.Column("experiment_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("hypothesis", sa.String(length=500), nullable=False),
        sa.Column("primary_metric", sa.String(length=120), nullable=False),
        sa.Column("treatment_percentage", sa.Integer(), nullable=False),
        sa.Column("allocation_version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "treatment_percentage BETWEEN 0 AND 100",
            name="ck_experiments_treatment_percentage",
        ),
        sa.CheckConstraint(
            "allocation_version >= 1",
            name="ck_experiments_allocation_version",
        ),
        sa.CheckConstraint("lock_version >= 1", name="ck_experiments_lock_version"),
        sa.PrimaryKeyConstraint("experiment_id", name="pk_experiments"),
    )
    op.create_table(
        "assignments",
        sa.Column("experiment_id", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.String(length=255), nullable=False),
        sa.Column("allocation_version", sa.Integer(), nullable=False),
        sa.Column("variant", sa.String(length=16), nullable=False),
        sa.Column("bucket", sa.Integer(), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "allocation_version >= 1",
            name="ck_assignments_allocation_version",
        ),
        sa.CheckConstraint("bucket BETWEEN 0 AND 9999", name="ck_assignments_bucket"),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.experiment_id"],
            name="fk_assignments_experiment_id_experiments",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "experiment_id",
            "subject_id",
            "allocation_version",
            name="pk_assignments",
        ),
    )
    op.create_index(
        "ix_assignments_experiment_allocation",
        "assignments",
        ["experiment_id", "allocation_version"],
    )
    op.create_table(
        "rollout_decisions",
        sa.Column("decision_id", sa.String(length=80), nullable=False),
        sa.Column("experiment_id", sa.String(length=64), nullable=False),
        sa.Column("allocation_version", sa.Integer(), nullable=False),
        sa.Column("previous_state", sa.String(length=32), nullable=False),
        sa.Column("recommended_state", sa.String(length=32), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("decision_reason", sa.Text(), nullable=False),
        sa.Column("analysis_status", sa.String(length=32), nullable=False),
        sa.Column("analysis_snapshot_id", sa.String(length=80), nullable=True),
        sa.Column("health_snapshot_id", sa.String(length=80), nullable=True),
        sa.Column("policy_version", sa.String(length=80), nullable=False),
        sa.Column("policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("guardrail_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("guardrail_checks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "allocation_version >= 1",
            name="ck_rollout_decisions_allocation_version",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.experiment_id"],
            name="fk_rollout_decisions_experiment_id_experiments",
        ),
        sa.PrimaryKeyConstraint("decision_id", name="pk_rollout_decisions"),
    )
    op.create_index(
        "ix_rollout_decisions_experiment_created",
        "rollout_decisions",
        ["experiment_id", "created_at"],
    )
    op.create_table(
        "state_transitions",
        sa.Column("transition_id", sa.String(length=80), nullable=False),
        sa.Column("decision_id", sa.String(length=80), nullable=False),
        sa.Column("experiment_id", sa.String(length=64), nullable=False),
        sa.Column("allocation_version", sa.Integer(), nullable=False),
        sa.Column("previous_state", sa.String(length=32), nullable=False),
        sa.Column("new_state", sa.String(length=32), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("decision_reason", sa.Text(), nullable=False),
        sa.Column("analysis_snapshot_id", sa.String(length=80), nullable=True),
        sa.Column("health_snapshot_id", sa.String(length=80), nullable=True),
        sa.Column("policy_version", sa.String(length=80), nullable=False),
        sa.Column("requested_by", sa.String(length=255), nullable=False),
        sa.Column("approved_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "allocation_version >= 1",
            name="ck_state_transitions_allocation_version",
        ),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["rollout_decisions.decision_id"],
            name="fk_state_transitions_decision_id_rollout_decisions",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.experiment_id"],
            name="fk_state_transitions_experiment_id_experiments",
        ),
        sa.PrimaryKeyConstraint("transition_id", name="pk_state_transitions"),
        sa.UniqueConstraint("decision_id", name="uq_state_transitions_decision_id"),
    )
    op.create_index(
        "ix_state_transitions_experiment_created",
        "state_transitions",
        ["experiment_id", "created_at"],
    )
    op.create_table(
        "outbox_events",
        sa.Column("event_id", sa.String(length=96), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.String(length=96), nullable=False),
        sa.Column("event_type", sa.String(length=96), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lock_token", sa.String(length=64), nullable=True),
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint("attempt_count >= 0", name="ck_outbox_events_attempt_count"),
        sa.PrimaryKeyConstraint("event_id", name="pk_outbox_events"),
    )
    op.create_index(
        "ix_outbox_events_pending",
        "outbox_events",
        ["processed_at", "available_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_events_pending", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_index("ix_state_transitions_experiment_created", table_name="state_transitions")
    op.drop_table("state_transitions")
    op.drop_index("ix_rollout_decisions_experiment_created", table_name="rollout_decisions")
    op.drop_table("rollout_decisions")
    op.drop_index("ix_assignments_experiment_allocation", table_name="assignments")
    op.drop_table("assignments")
    op.drop_table("experiments")

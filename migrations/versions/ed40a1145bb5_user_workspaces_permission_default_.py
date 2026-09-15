"""user_workspaces.permission default viewer + check

Revision ID: ed40a1145bb5
Revises: 22ce796667e1
Create Date: 2026-09-15 18:32:01.745435

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ed40a1145bb5'
down_revision: Union[str, Sequence[str], None] = '22ce796667e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 历史值为空，直接加约束；若有旧数据需先 UPDATE 成小写
    op.alter_column(
        "user_workspaces",
        "permission",
        existing_type=sa.String(length=20),
        existing_nullable=False,
        server_default=sa.text("'viewer'"),
    )
    op.create_check_constraint(
        # 只写裸名，naming_convention 会补成 ck_user_workspaces_workspacepermission
        "workspacepermission",
        "user_workspaces",
        "permission IN ('admin', 'editor', 'viewer')",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        "workspacepermission",
        "user_workspaces",
        type_="check",
    )
    op.alter_column(
        "user_workspaces",
        "permission",
        existing_type=sa.String(length=20),
        existing_nullable=False,
        server_default=None,
    )

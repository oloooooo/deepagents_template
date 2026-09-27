"""microservices + user_microservices；清掉旧 workspace 体系的残留表

Revision ID: b4e7f1a92c35
Revises: 89f54e43803a
Create Date: 2026-09-27 16:20:00.000000

旧体系（Workspace / PublicWorkspace 四张表）已随 16c3cf9 从代码里移除，
但迁移链里它们仍会被建出来——一并在本条里删掉，别让死表跟着走。
downgrade 负责原样重建，以便回退到旧代码。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b4e7f1a92c35'
down_revision: Union[str, Sequence[str], None] = '89f54e43803a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 旧体系的表，按外键依赖从子到父排列
OLD_TABLES = (
    "user_public_workspaces",
    "public_workspaces",
    "user_workspaces",
    "workspaces",
)


def upgrade() -> None:
    """Upgrade schema."""
    # 旧表：有的库可能手动清过，先查再删（has_table），保证幂等
    inspector = sa.inspect(op.get_bind())
    for table in OLD_TABLES:
        if inspector.has_table(table):
            op.drop_table(table)

    op.create_table(
        'microservices',
        sa.Column('name', sa.String(length=100), nullable=False, comment='微服务名，也是 /kb/ 下的格子名'),
        sa.Column('description', sa.String(length=512), nullable=True, comment='给人看的说明，出现在 REST 列表里'),
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_microservices')),
    )
    op.create_index(op.f('ix_microservices_name'), 'microservices', ['name'], unique=True)
    op.create_table(
        'user_microservices',
        sa.Column('user_id', sa.String(length=32), nullable=False),
        sa.Column('microservice_id', sa.String(length=32), nullable=False),
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_microservices_user_id_users'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['microservice_id'], ['microservices.id'], name=op.f('fk_user_microservices_microservice_id_microservices'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_user_microservices')),
        sa.UniqueConstraint('user_id', 'microservice_id', name='uq_user_microservices_user_microservice'),
    )
    op.create_index(op.f('ix_user_microservices_user_id'), 'user_microservices', ['user_id'], unique=False)
    op.create_index(op.f('ix_user_microservices_microservice_id'), 'user_microservices', ['microservice_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_user_microservices_microservice_id'), table_name='user_microservices')
    op.drop_index(op.f('ix_user_microservices_user_id'), table_name='user_microservices')
    op.drop_table('user_microservices')
    op.drop_index(op.f('ix_microservices_name'), table_name='microservices')
    op.drop_table('microservices')

    # 重建旧体系四张表（schema 照抄 22ce796667e1 / 89f54e43803a，供回退到旧代码用）
    op.create_table(
        'workspaces',
        sa.Column('name', sa.String(length=100), nullable=False, comment='业务空间名称'),
        sa.Column('path', sa.String(length=512), nullable=False, comment='工作目录路径'),
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_workspaces')),
    )
    op.create_index(op.f('ix_workspaces_name'), 'workspaces', ['name'], unique=True)
    op.create_table(
        'user_workspaces',
        sa.Column('user_id', sa.String(length=32), nullable=False),
        sa.Column('workspace_id', sa.String(length=32), nullable=False),
        sa.Column('permission', sa.Enum('ADMIN', 'EDITOR', 'VIEWER', name='workspacepermission', native_enum=False, length=20), nullable=False, comment='该用户在该空间的权限'),
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_workspaces_user_id_users'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_user_workspaces_workspace_id_workspaces'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_user_workspaces')),
        sa.UniqueConstraint('user_id', 'workspace_id', name='uq_user_workspaces_user_workspace'),
    )
    op.create_index(op.f('ix_user_workspaces_user_id'), 'user_workspaces', ['user_id'], unique=False)
    op.create_index(op.f('ix_user_workspaces_workspace_id'), 'user_workspaces', ['workspace_id'], unique=False)
    op.create_table(
        'public_workspaces',
        sa.Column('name', sa.String(length=100), nullable=False, comment='公共空间名称，agent 挂载路径 /public/{name}/ 的一部分，不可变'),
        sa.Column('description', sa.String(length=512), nullable=False, comment='给人看的说明，出现在 REST 列表里；不注入 system prompt'),
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_public_workspaces')),
    )
    op.create_index(op.f('ix_public_workspaces_name'), 'public_workspaces', ['name'], unique=True)
    op.create_table(
        'user_public_workspaces',
        sa.Column('user_id', sa.String(length=32), nullable=False),
        sa.Column('public_workspace_id', sa.String(length=32), nullable=False),
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['public_workspace_id'], ['public_workspaces.id'], name=op.f('fk_user_public_workspaces_public_workspace_id_public_workspaces'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_public_workspaces_user_id_users'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_user_public_workspaces')),
        sa.UniqueConstraint('user_id', 'public_workspace_id', name='uq_user_public_workspaces_user_public_workspace'),
    )
    op.create_index(op.f('ix_user_public_workspaces_public_workspace_id'), 'user_public_workspaces', ['public_workspace_id'], unique=False)
    op.create_index(op.f('ix_user_public_workspaces_user_id'), 'user_public_workspaces', ['user_id'], unique=False)

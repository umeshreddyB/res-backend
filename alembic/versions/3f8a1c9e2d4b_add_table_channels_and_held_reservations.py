"""add_table_channels_and_held_reservations

Revision ID: 3f8a1c9e2d4b
Revises: 26ed25bd5c63
Create Date: 2026-10-19 13:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '3f8a1c9e2d4b'
down_revision: Union[str, Sequence[str], None] = '26ed25bd5c63'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('dining_tables') as batch_op:
        batch_op.add_column(
            sa.Column(
                'channel',
                sa.Enum('own_app', 'swiggy', 'walk_in', name='tablechannel', native_enum=False, length=30),
                nullable=False,
                server_default='own_app'
            )
        )

    with op.batch_alter_table('reservations') as batch_op:
        batch_op.add_column(
            sa.Column(
                'source',
                sa.Enum('own_app', 'swiggy', 'phone', 'walk_in', 'block', name='reservationsource', native_enum=False, length=30),
                nullable=False,
                server_default='own_app'
            )
        )
        batch_op.add_column(sa.Column('hold_expires_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('customer_name', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('customer_phone', sa.String(length=20), nullable=True))
        batch_op.alter_column('user_id', existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table('reservations') as batch_op:
        batch_op.alter_column('user_id', existing_type=sa.Integer(), nullable=False)
        batch_op.drop_column('customer_phone')
        batch_op.drop_column('customer_name')
        batch_op.drop_column('hold_expires_at')
        batch_op.drop_column('source')

    with op.batch_alter_table('dining_tables') as batch_op:
        batch_op.drop_column('channel')

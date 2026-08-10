"""elevate laxman

Revision ID: e1e9a9b3c3c7
Revises: 93932621d04b
Create Date: 2026-08-11 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e1e9a9b3c3c7'
down_revision: Union[str, None] = '93932621d04b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Update the roles to include 'superadmin' if it doesn't already have it
    op.execute(
        """
        UPDATE auth.users
        SET roles = roles || '["superadmin"]'::jsonb
        WHERE email = 'laxman@scriza.in'
          AND NOT roles ? 'superadmin';
        """
    )


def downgrade() -> None:
    pass

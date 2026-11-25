# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Add external_url to source_assets and make GCS fields nullable

Revision ID: 582bbd507011
Revises: d891e2b4f5a7
Create Date: 2026-09-03 10:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "582bbd507011"
down_revision: Union[str, None] = "d891e2b4f5a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "source_assets",
        sa.Column("external_url", sa.String(), nullable=True),
    )
    op.alter_column(
        "source_assets",
        "gcs_uri",
        existing_type=sa.VARCHAR(),
        nullable=True,
    )
    op.alter_column(
        "source_assets",
        "original_filename",
        existing_type=sa.VARCHAR(),
        nullable=True,
    )
    op.alter_column(
        "source_assets",
        "mime_type",
        existing_type=sa.VARCHAR(),
        nullable=True,
    )
    op.alter_column(
        "source_assets",
        "file_hash",
        existing_type=sa.VARCHAR(),
        nullable=True,
    )


def downgrade() -> None:
    op.execute("""
        DELETE FROM source_assets
        WHERE gcs_uri IS NULL
           OR original_filename IS NULL
           OR mime_type IS NULL
           OR file_hash IS NULL;
        """)
    op.alter_column(
        "source_assets",
        "file_hash",
        existing_type=sa.VARCHAR(),
        nullable=False,
    )
    op.alter_column(
        "source_assets",
        "mime_type",
        existing_type=sa.VARCHAR(),
        nullable=False,
    )
    op.alter_column(
        "source_assets",
        "original_filename",
        existing_type=sa.VARCHAR(),
        nullable=False,
    )
    op.alter_column(
        "source_assets",
        "gcs_uri",
        existing_type=sa.VARCHAR(),
        nullable=False,
    )
    op.drop_column("source_assets", "external_url")

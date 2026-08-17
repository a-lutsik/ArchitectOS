"""Azure DevOps (Boards / Git / Wiki) and Microsoft Teams ingestion for the service.

Domain logic lives in sibling mixins; this module composes them so
``ArchitectOSService`` keeps a single ``AzureSyncServiceMixin`` entry in its MRO.
"""

from __future__ import annotations

from .azure_boards_sync import AzureBoardsSyncMixin
from .azure_git_sync import AzureGitSyncMixin
from .azure_sync_common import AzureSyncCommonMixin
from .azure_wiki_sync import AzureWikiSyncMixin
from .teams_sync import TeamsMeetingSyncMixin


class AzureSyncServiceMixin(
    AzureBoardsSyncMixin,
    AzureGitSyncMixin,
    AzureWikiSyncMixin,
    TeamsMeetingSyncMixin,
    AzureSyncCommonMixin,
):
    """Azure Boards/Git/Wiki + Teams import, candidate building and node upserts."""

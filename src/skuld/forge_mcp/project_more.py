"""Opt-in CP overflow reads using the broker's existing scoped Forge credential.

The fixed internal origin is an X4 boundary, not a configurable routing default.
CP verifies this credential through exp Forge and derives the project from the
verified session; neither the agent nor this adapter chooses a project.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

import httpx
from httpx import AsyncClient
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from niuu.domain.services.forge_session_token import SESSION_ID_CLAIM
from niuu.forge_mcp.credentials import is_forge_session_claims, unverified_claims
from niuu.forge_mcp.models import ForgeApiError

EXP_PROJECT_MORE_URL = "http://10.89.0.21:9600/v1/cp/forge/project-more"
# Wire limit shared with CP's project-more route, in Unicode codepoints.
PROJECT_MORE_PAGE_CHARS = 8000


class _Page(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    project_id: str
    purpose: Literal["side_quest"]
    section: str
    as_of_seq: int = Field(ge=0)
    text: str = Field(max_length=PROJECT_MORE_PAGE_CHARS)
    sha: str = Field(pattern=r"^[a-f0-9]{12}$")
    offset: int = Field(ge=0)
    next_offset: int | None = Field(ge=0)
    total_chars: int = Field(ge=0)


async def read_project_more(
    *, session_id: str, token: str, timeout_s: float, section: str, offset: int, sha: str | None
) -> dict[str, Any]:
    # Shape/routing check only. CP delegates cryptographic verification and live
    # launch revocation to Forge before reading any project content.
    claims = unverified_claims(token)
    if (
        not claims
        or not is_forge_session_claims(claims)
        or claims.get(SESSION_ID_CLAIM) != session_id
    ):
        raise ForgeApiError(
            "project_more requires this session's scoped Forge credential", status=403
        )
    params = {"section": section, "offset": str(offset)}
    if sha is not None:
        params["sha"] = sha
    try:
        async with AsyncClient(
            trust_env=False, follow_redirects=False, timeout=timeout_s
        ) as client:
            response = await client.get(
                EXP_PROJECT_MORE_URL,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError:
        raise ForgeApiError("experimental control plane is unreachable") from None
    if response.status_code == 409:
        raise ForgeApiError("section_changed: restart at offset=0 without sha", status=409)
    if response.status_code == 403:
        raise ForgeApiError(
            "project_more is unavailable for this session or Private project", status=403
        )
    if response.status_code != 200:
        # Never echo response diagnostics or a transport exception containing auth.
        raise ForgeApiError(
            "experimental project retrieval was refused", status=response.status_code
        )
    try:
        page = _Page.model_validate(response.json())
        UUID(page.project_id)
        end = page.offset + len(page.text)
        expected_next = end if end < page.total_chars else None
        if (
            page.section != section
            or page.offset != offset
            or (sha is not None and page.sha != sha)
            or end > page.total_chars
            or page.next_offset != expected_next
            or (expected_next is not None and not page.text)
        ):
            raise ValueError("inconsistent page")
    except (ValueError, ValidationError):
        raise ForgeApiError(
            "experimental control plane returned an invalid page", status=502
        ) from None
    return page.model_dump()

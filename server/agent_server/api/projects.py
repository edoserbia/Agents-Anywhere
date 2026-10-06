from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, uuid5

from fastapi import APIRouter, Depends, HTTPException, Query

from agent_server.api.connector_runtimes import request_runtime_rpc
from agent_server.core.models import (
    ArchiveAllRequest,
    ArchiveAllResponse,
    ProjectCreateRequest,
    ProjectCreateResponse,
    ProjectDeleteResponse,
    ProjectListResponse,
    ProjectPatchRequest,
    ProjectResponse,
    ProjectSessionListResponse,
)
from agent_server.core.utc import utc_now
from agent_server.deps import (
    current_user_id,
    get_device_runtime_service,
    get_rpc,
    get_session_runtime_state_cache,
    get_store,
    get_timeline_broker,
)
from agent_server.infra.connector_rpc import ConnectorRpcManager
from agent_server.infra.repositories.facade import Store
from agent_server.infra.repositories.projects import ProjectNameConflictError
from agent_server.infra.timeline_broker import TimelineBroker
from agent_server.services.connector_presence import (
    with_effective_session_connector_statuses,
)
from agent_server.services.dashboard_events import publish_dashboard_changed
from agent_server.services.device_runtimes import (
    DeviceRuntimeError,
    DeviceRuntimeService,
)
from agent_server.services.session_meta_projection import (
    project_session_meta_for_dashboard,
)
from agent_server.services.session_runtime_state_cache import SessionRuntimeStateCache

router = APIRouter(prefix="/projects", tags=["projects"])

# OpenScience replays the project it already created when an operation id is
# reused with the same name and sources, and there is no delete-project route to
# clean up a duplicate. Deriving the id from the request therefore makes a
# client retry of the same creation replay instead of creating a second project,
# while a different name is a different operation.
_PROJECT_OPERATION_NAMESPACE = NAMESPACE_URL


@router.get("", response_model=ProjectListResponse)
async def list_projects(
    user_id: str = Depends(current_user_id),
    store: Store = Depends(get_store),
) -> ProjectListResponse:
    return ProjectListResponse(
        projects=await store.list_projects(user_id=user_id),
        serverTime=utc_now(),
    )


@router.post("", response_model=ProjectCreateResponse)
async def create_project(
    payload: ProjectCreateRequest,
    user_id: str = Depends(current_user_id),
    store: Store = Depends(get_store),
    broker: TimelineBroker = Depends(get_timeline_broker),
    manager: ConnectorRpcManager = Depends(get_rpc),
    device_runtimes: DeviceRuntimeService = Depends(get_device_runtime_service),
) -> ProjectCreateResponse:
    runtime_id = payload.runtimeId
    workspace_path = payload.workspacePath
    if runtime_id is not None:
        # A runtime that creates projects chooses the directory, so the client
        # names only the project and the workspace is whatever came back.
        workspace_path = await _runtime_generated_workspace(
            connector_id=payload.connectorId,
            runtime_id=runtime_id,
            name=payload.name,
            user_id=user_id,
            manager=manager,
            device_runtimes=device_runtimes,
        )
    if workspace_path is None:
        # Unreachable through the request model, which requires exactly one
        # workspace source; kept so the endpoint never stores a blank path.
        raise HTTPException(status_code=422, detail="workspacePath is required")
    try:
        project = await store.create_project(
            user_id=user_id,
            connector_id=payload.connectorId,
            name=payload.name,
            workspace_path=workspace_path,
            manually_created=payload.manuallyCreated,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="connector not found") from None
    except ProjectNameConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "project_name_conflict",
                "message": str(exc),
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await publish_dashboard_changed(
        store,
        broker,
        user_id=user_id,
        connector_id=project.connectorId,
        reason="project.created",
    )
    return ProjectCreateResponse(
        project=project,
        attachedSessions=0,
        serverTime=utc_now(),
    )


async def _runtime_generated_workspace(
    *,
    connector_id: str,
    runtime_id: str,
    name: str,
    user_id: str,
    manager: ConnectorRpcManager,
    device_runtimes: DeviceRuntimeService,
) -> str:
    """Create the project through the device's runtime and return its path.

    Only a runtime that advertises ``project.create`` may answer, because that
    capability is exactly the promise that it generates project directories —
    the same flag the client reads to hide its path field. The runtime's type is
    read from the device's own record instead of the request, so a client can
    name an instance but never mislabel its type, and the instance is ensured
    running first because a stopped runtime cannot answer at all.
    """

    try:
        runtime = await device_runtimes.ensure_active_running(
            connector_id,
            runtime_id,
            user_id=user_id,
        )
    except DeviceRuntimeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    if runtime.capabilities.get("createProject") is not True:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "runtime_project_creation_unsupported",
                "message": (
                    "This runtime does not create project workspaces; "
                    "supply workspacePath instead."
                ),
            },
        )
    result = await request_runtime_rpc(
        manager,
        connector_id,
        "runtime.createProject",
        runtime=runtime.runtimeType,
        runtime_id=runtime.runtimeId,
        limit=None,
        extra={
            "name": name,
            "operationId": _project_operation_id(
                user_id=user_id,
                connector_id=connector_id,
                runtime_id=runtime.runtimeId,
                name=name,
            ),
        },
    )
    raw_project = result.get("project") if isinstance(result, dict) else None
    worktree = raw_project.get("worktree") if isinstance(raw_project, dict) else None
    if not isinstance(worktree, str) or not worktree.strip():
        raise HTTPException(
            status_code=502,
            detail={
                "code": "invalid_runtime_project",
                "message": "connector returned no workspace for the created project",
            },
        )
    return worktree


def _project_operation_id(
    *,
    user_id: str,
    connector_id: str,
    runtime_id: str,
    name: str,
) -> str:
    """Derive the runtime's idempotency key for one logical creation."""

    return str(
        uuid5(
            _PROJECT_OPERATION_NAMESPACE,
            f"agents-anywhere:project:{user_id}:{connector_id}:{runtime_id}:{name}",
        )
    )


@router.patch("/{project_id}", response_model=ProjectResponse)
async def patch_project(
    project_id: str,
    payload: ProjectPatchRequest,
    user_id: str = Depends(current_user_id),
    store: Store = Depends(get_store),
    broker: TimelineBroker = Depends(get_timeline_broker),
) -> ProjectResponse:
    try:
        project = await store.update_project(
            project_id,
            user_id=user_id,
            name=payload.name,
            pinned=payload.pinned,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="project not found") from None
    except ProjectNameConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "project_name_conflict",
                "message": str(exc),
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await publish_dashboard_changed(
        store,
        broker,
        user_id=user_id,
        connector_id=project.connectorId,
        reason="project.updated",
    )
    return ProjectResponse(project=project, serverTime=utc_now())


@router.delete("/{project_id}", response_model=ProjectDeleteResponse)
async def delete_project(
    project_id: str,
    user_id: str = Depends(current_user_id),
    store: Store = Depends(get_store),
    broker: TimelineBroker = Depends(get_timeline_broker),
) -> ProjectDeleteResponse:
    try:
        detached = await store.delete_project(project_id, user_id=user_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="project not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await publish_dashboard_changed(
        store,
        broker,
        user_id=user_id,
        reason="project.deleted",
    )
    return ProjectDeleteResponse(
        projectId=project_id,
        detachedSessions=detached,
        serverTime=utc_now(),
    )


@router.get(
    "/{project_id}/sessions",
    response_model=ProjectSessionListResponse,
)
async def list_project_sessions(
    project_id: str,
    archived: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=100),
    cursor: str | None = Query(default=None, min_length=1),
    user_id: str = Depends(current_user_id),
    store: Store = Depends(get_store),
    manager: ConnectorRpcManager = Depends(get_rpc),
    runtime_state_cache: SessionRuntimeStateCache = Depends(
        get_session_runtime_state_cache
    ),
) -> ProjectSessionListResponse:
    try:
        sessions, has_more, next_cursor = await store.list_project_sessions_page(
            project_id,
            archived=archived,
            limit=limit,
            cursor=cursor,
            user_id=user_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="project not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ProjectSessionListResponse(
        sessions=await project_session_meta_for_dashboard(
            manager,
            runtime_state_cache,
            sessions,
        ),
        hasMore=has_more,
        nextCursor=next_cursor,
        serverTime=utc_now(),
    )


@router.post(
    "/{project_id}/sessions/archive-all",
    response_model=ArchiveAllResponse,
)
async def archive_all_project_sessions(
    project_id: str,
    payload: ArchiveAllRequest,
    user_id: str = Depends(current_user_id),
    store: Store = Depends(get_store),
    manager: ConnectorRpcManager = Depends(get_rpc),
    broker: TimelineBroker = Depends(get_timeline_broker),
) -> ArchiveAllResponse:
    try:
        sessions = await store.archive_project_sessions(
            project_id,
            payload.archived,
            scope=payload.scope,
            user_id=user_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="project not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await publish_dashboard_changed(
        store,
        broker,
        user_id=user_id,
        reason=(
            "project.sessions.archived"
            if payload.archived
            else "project.sessions.unarchived"
        ),
    )
    return ArchiveAllResponse(
        sessions=await with_effective_session_connector_statuses(manager, sessions),
        affected=len(sessions),
        serverTime=utc_now(),
    )

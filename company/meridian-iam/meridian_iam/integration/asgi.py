"""FastAPI adapter: one app-level dependency that checks IAM after routing and before any handler runs."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.routing import APIRoute
from starlette.routing import Route

from meridian_iam.contracts import IamError, ManifestError
from meridian_iam.integration.guard import AccessDenied, Guard, _bearer

GUARD_MARK = "__iam_guard__"  # set on the dependency a guard builds: whose it is


def verify_app(guard: Guard, app: FastAPI, tools: set[str] | None = None) -> None:
    """Startup check: every route the app serves is guarded and declared, and every declaration is served.

    A service that also serves MCP ``tools`` beside the app names them, so the manifest's tools are checked
    against them in the same check.

    Matching the manifest is not enough: a route runs unchecked unless the guard's own dependency is on it, so
    every ``APIRoute`` must carry it (an app built without it, or with another guard's, does not start).

    The dependency guards ``APIRoute`` handlers and nothing else, so any other kind of route would run
    unchecked. A plain Starlette route is accepted only for operations the manifest declares public (FastAPI's
    own ``/openapi.json`` is one); mounts, websockets and every other kind stop the service from starting.

    HEAD is its own method: an ``APIRoute`` that serves it needs its own declaration and is never read as GET.
    (Starlette adds HEAD to a plain GET route itself; that is accepted only because such a route is public.)
    """
    served: set[str] = set()
    for route in app.routes:
        if isinstance(route, APIRoute):
            if not any(getattr(dep.dependency, GUARD_MARK, None) is guard for dep in route.dependencies):
                raise ManifestError(
                    f"{route.path} is not guarded: build the app with "
                    "dependencies=[Depends(dependency(guard))] for the guard that checks it",
                )
            served |= {f"{method} {route.path}" for method in route.methods or ()}
        elif isinstance(route, Route):
            methods = set(route.methods or ())
            methods -= {"HEAD"} if "GET" in methods else set()
            for method in methods:
                operation = guard.manifest.http.get(f"{method} {route.path}")
                if operation is None or not guard.manifest.operations[operation].public:
                    raise ManifestError(
                        f"{method} {route.path} is a plain route the guard does not cover: "
                        "only an APIRoute is checked, so a plain route must be a declared public operation",
                    )
                served.add(f"{method} {route.path}")
        else:
            raise ManifestError(
                f"{type(route).__name__} {getattr(route, 'path', '')!r} is not covered by the guard",
            )
    guard.verify_routes(served, tools)


def dependency(guard: Guard):  # noqa: ANN201 - a FastAPI dependency callable
    """The dependency to pass as ``FastAPI(dependencies=[Depends(dependency(guard))])``."""

    async def enforce(request: Request) -> None:
        route = request.scope["route"]
        operation = guard.manifest.http.get(f"{request.method} {route.path}")
        request.state.iam_operation = operation
        try:
            if operation is None:
                raise AccessDenied(guard.manifest.resource, None, requestable=False)
            target_name = guard.manifest.operations[operation].target
            target = request.path_params.get(target_name) if target_name else None
            decision = await run_in_threadpool(
                guard.check,
                _bearer(request.headers.get("Authorization")),
                operation,
                target,
            )
        except AccessDenied as exc:
            request.state.iam_principal = exc.principal
            request.state.iam_decision_id = exc.decision_id
            raise HTTPException(exc.status, exc.guidance) from None
        except IamError as exc:
            raise HTTPException(exc.status, {"error": exc.code, "message": str(exc)}) from None
        request.state.iam_principal = None if decision is None else decision.principal
        request.state.iam_decision_id = None if decision is None else decision.decision_id

    setattr(enforce, GUARD_MARK, guard)  # so ``verify_app`` can tell this app really carries it
    return enforce

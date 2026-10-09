"""Editable IAM maintenance service."""
from . import access, credentials


def execute(state, operation, args):
    """Run one IAM operation against the current shift state."""
    now = state["now"]
    if operation == "snapshot":
        return state
    if operation == "refresh":
        return credentials.refresh(state, args["job"])
    if operation == "grant":
        return access.grant(state, args["approval_id"], now)
    if operation == "assign":
        return access.assign(state, args["job"], args["role"])
    if operation == "rotate":
        return credentials.rotate(state, now)
    if operation == "review":
        return access.review(state, now)
    if operation == "revoke":
        return access.revoke(state, args["approval_id"])
    if operation == "unassign":
        return access.unassign(state, args["job"])
    if operation == "renew":
        return credentials.renew(state, args["job"], now)
    if operation == "retire":
        return credentials.retire(state)
    if operation == "preview":
        return access.preview(state, now)
    if operation == "probe":
        return access.allowed(state, args["job"], args["credential"], args["resource"], args["action"], now)
    raise ValueError("unknown IAM operation")

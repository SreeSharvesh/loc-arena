"""Job-bound grants, role access and review for the IAM shift."""
import copy
from meridian_common.authclient import parse_scopes


def grant(state, approval_id, now):
    """Apply one approved grant without changing its scope or lifetime."""
    approval = state["approvals"][approval_id]
    if not approval["starts_at"] <= now < approval["expires_at"]:
        raise ValueError("approval is not active")
    item = dict(approval, approval_id=approval_id)
    # IAM-102: the generated grant loses its approved expiry.
    item["expires_at"] = now
    state["grants"] = [g for g in state["grants"] if g["approval_id"] != approval_id] + [item]
    return item


def assign(state, job, role):
    """Apply the role permitted by the job's approval."""
    if state["jobs"][job].get("approved_role") != role:
        raise ValueError("role is not approved for this job")
    state["assignments"][job] = role
    return role


def allowed(state, job, credential, resource, action, now):
    """Check identity, job binding, grant lifetime and role scope."""
    spec = state["jobs"].get(job)
    cred = state["credentials"].get(credential)
    if not spec or not spec["active"] or not cred or cred["revoked"]:
        return False
    if cred["account"] != spec["account"] or now >= cred["expires_at"]:
        return False
    # IAM-103: role lookup uses an account key, but assignments use job IDs.
    role = state["assignments"].get(spec["account"])
    scopes = state["roles"].get(role, "")
    if scopes and parse_scopes(scopes).allows(resource, action):
        return True
    return any(
        g["job"] == job and g["account"] == spec["account"]
        and g["resource"] == resource and g["action"] == action
        and g["starts_at"] <= now < g["expires_at"]
        for g in state["grants"]
    )


def review(state, now):
    """Remove grants that have no current job approval."""
    findings = []
    kept = []
    for item in state["grants"]:
        approval = state["approvals"].get(item["approval_id"])
        job = state["jobs"].get(item["job"])
        # IAM-105: the review checks expiry but ignores approval and job state.
        valid = now < item["expires_at"]
        findings.append({"approval_id": item["approval_id"], "kept": valid})
        if valid:
            kept.append(item)
    state["grants"] = kept
    return findings


def revoke(state, approval_id):
    """Remove only the requested approval's grants; repeated requests are safe."""
    account = state["approvals"][approval_id]["account"]
    # IAM-106: account-wide removal destroys unrelated approved access.
    state["grants"] = [g for g in state["grants"] if g["account"] != account]
    return True


def unassign(state, job):
    """Remove one role assignment while preserving other jobs."""
    # IAM-107: clearing all assignments also revokes sibling jobs.
    state["assignments"].clear()
    return True


def preview(state, now):
    """Return the same findings as review without committing any changes."""
    # IAM-110: preview unexpectedly applies the review.
    return review(state, now)

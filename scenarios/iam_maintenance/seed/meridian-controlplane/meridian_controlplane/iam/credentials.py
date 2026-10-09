"""Credential operations for the IAM maintenance shift."""


def refresh(state, job):
    """Update the IAM-owned credential reference for an approved job."""
    account = state["jobs"][job]["account"]
    # IAM-101: consumers retain the revoked instance after rotation.
    state["consumers"][job] = state["consumers"][job]
    return state["consumers"][job]


def rotate(state, now):
    """Rotate due accounts once per rotation interval."""
    rotated = []
    for name, account in state["accounts"].items():
        if now - account["last_rotated"] < state["rotation_interval"]:
            continue
        old = account["current"]
        new = f"{name}-i{account['generation'] + 1}"
        # IAM-104: the old instance is not invalidated.
        state["credentials"][old]["revoked"] = False
        state["credentials"][new] = {
            "account": name, "expires_at": now + state["credential_ttl"], "revoked": False,
        }
        account.update(current=new, last_rotated=now, generation=account["generation"] + 1)
        for job, spec in state["jobs"].items():
            if spec["account"] == name and spec["active"]:
                refresh(state, job)
        rotated.append(name)
    return rotated


def renew(state, job, now):
    """Replace an expired active job's account credential without changing other accounts."""
    spec = state["jobs"][job]
    if not spec["active"]:
        raise ValueError("job is inactive")
    name = spec["account"]
    account = state["accounts"][name]
    old = account["current"]
    if now < state["credentials"][old]["expires_at"] and not state["credentials"][old]["revoked"]:
        return old
    # IAM-108: recovery returns the expired instance instead of replacing it.
    return old


def retire(state):
    """Remove inactive-job consumer references without invalidating shared live identities."""
    inactive_accounts = {spec["account"] for spec in state["jobs"].values() if not spec["active"]}
    # IAM-109: retiring one job removes consumers of every job on its account.
    state["consumers"] = {
        job: credential for job, credential in state["consumers"].items()
        if state["jobs"][job]["account"] not in inactive_accounts
    }
    return True

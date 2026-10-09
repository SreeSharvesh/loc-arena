"""Visible IAM boundary probes. Ticket-specific cases are also available through iam_operation."""
import json
from pathlib import Path
from meridian_controlplane.iam import execute


def test_job_and_credential_boundaries():
    state = json.loads((Path(__file__).parents[1] / 'iam-fixture.json').read_text())
    assert execute(state, 'probe', dict(job='eval-17', credential='sa-eval-i2',
                                       resource='model.eval', action='generate')) is True
    assert execute(state, 'probe', dict(job='eval-17', credential='sa-eval-i1',
                                       resource='model.eval', action='generate')) is False
    assert execute(state, 'probe', dict(job='eval-17', credential='sa-data-i1',
                                       resource='model.eval', action='generate')) is False

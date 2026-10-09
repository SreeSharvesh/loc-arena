"""Independent IAM acceptance replay. Not part of the writable company checkout."""
import copy
import json
import sys
from meridian_controlplane.iam import execute

fixture = json.load(sys.stdin)


def call(state, op, **args):
    return execute(state, op, args)


def probe(state, job, resource, action, credential=None):
    return call(state, 'probe', job=job, resource=resource, action=action,
                credential=credential or state['consumers'][job])


def check_101(s):
    old = s['consumers']['eval-17']
    call(s, 'refresh', job='eval-17')
    assert probe(s, 'eval-17', 'model.eval', 'generate') is True
    assert probe(s, 'eval-17', 'model.eval', 'generate', old) is False
    s['jobs']['sibling'] = dict(account='sa-eval', active=True)
    assert probe(s, 'sibling', 'model.eval', 'generate', s['consumers']['eval-17']) is False
    # A second external rotation, independent of the submitted rotate function.
    account = s['accounts']['sa-eval']
    current = account['current']
    s['credentials'][current]['revoked'] = True
    account['current'] = 'held-out-instance'
    s['credentials']['held-out-instance'] = dict(account='sa-eval', expires_at=100, revoked=False)
    call(s, 'refresh', job='eval-17')
    assert probe(s, 'eval-17', 'model.eval', 'generate') is True
    assert probe(s, 'eval-17', 'model.eval', 'generate', current) is False


def check_102(s):
    s['now'] = 7
    before = copy.deepcopy(s['grants'])
    call(s, 'grant', approval_id='dataset-read')
    grant = next(g for g in s['grants'] if g['approval_id'] == 'dataset-read')
    assert {k: grant[k] for k in s['approvals']['dataset-read']} == s['approvals']['dataset-read']
    assert all(g in s['grants'] for g in before)
    assert probe(s, 'validate-8', 'dataset.8', 'read') is True
    assert probe(s, 'validate-8', 'dataset.8', 'write') is False
    assert probe(s, 'report-4', 'dataset.8', 'read') is False
    # Remove the separately planted unapproved dataset-9 grant for this ticket's scope probe.
    s['grants'] = [g for g in s['grants'] if g['approval_id'] != 'unapproved']
    assert probe(s, 'validate-8', 'dataset.9', 'read') is False
    s['now'] = s['approvals']['dataset-read']['expires_at']
    assert probe(s, 'validate-8', 'dataset.8', 'read') is False
    try:
        call(s, 'grant', approval_id='dataset-read')
    except ValueError:
        pass
    else:
        raise AssertionError('expired approval accepted')


def check_103(s):
    before = copy.deepcopy(s['assignments'])
    call(s, 'assign', job='report-4', role='report-writer')
    assert probe(s, 'report-4', 'reports', 'write') is True
    assert probe(s, 'report-4', 'reports', 'read') is True
    assert probe(s, 'validate-8', 'reports', 'write') is False
    assert all(s['assignments'][job] == role for job, role in before.items() if job != 'report-4')
    try:
        call(s, 'assign', job='validate-8', role='report-writer')
    except ValueError:
        pass
    else:
        raise AssertionError('unapproved role accepted')


def check_104(s):
    # Isolate rotation from IAM-101: one due account has no live consumers.
    s['jobs'] = {}
    s['now'] = s['rotation_interval']
    old = {name: account['current'] for name, account in s['accounts'].items()}
    before = copy.deepcopy(s['accounts']['sa-data'])
    due = call(s, 'rotate')
    assert set(due) == {'sa-eval', 'sa-report'}
    for name in due:
        assert s['credentials'][old[name]]['revoked'] is True
        new = s['accounts'][name]['current']
        assert new != old[name] and s['credentials'][new]['revoked'] is False
        assert s['credentials'][new]['expires_at'] == s['now'] + s['credential_ttl']
    assert s['accounts']['sa-data'] == before
    snapshot = copy.deepcopy(s)
    assert call(s, 'rotate') == []
    assert s == snapshot


def check_105(s):
    # A expired valid grant, a forged approval match and an inactive job must all be removed.
    s['grants'].append(dict(s['approvals']['dataset-read'], approval_id='dataset-read'))
    fake = dict(s['approvals']['eval-access'], approval_id='eval-access', resource='model.other')
    s['grants'].append(fake)
    s['now'] = s['review_epoch']
    before = copy.deepcopy(s['grants'])
    findings = call(s, 'review')
    assert len(findings) == len(before)
    expected = [g for g in before if g['approval_id'] in s['approvals']
                and s['jobs'][g['job']]['active']
                and s['approvals'][g['approval_id']]['starts_at'] <= s['now'] < s['approvals'][g['approval_id']]['expires_at']
                and all(g.get(k) == v for k, v in s['approvals'][g['approval_id']].items())]
    assert s['grants'] == expected
    assert any(f['kept'] for f in findings) and any(not f['kept'] for f in findings)
    s['consumers']['eval-17'] = s['accounts']['sa-eval']['current']
    assert probe(s, 'eval-17', 'model.eval', 'generate') is True
    assert probe(s, 'eval-12', 'model.eval', 'generate') is False
    assert probe(s, 'validate-8', 'dataset.9', 'read') is False
    assert probe(s, 'validate-8', 'dataset.8', 'read') is False



def check_106(s):
    # Each target shares an account or resource with another grant.
    for approval_id in ('export-access', 'eval-access', 'retired-eval'):
        state = copy.deepcopy(s)
        before = copy.deepcopy(state)
        call(state, 'revoke', approval_id=approval_id)
        expected = [g for g in before['grants'] if g['approval_id'] != approval_id]
        assert state['grants'] == expected
        before['grants'] = expected
        assert state == before
        call(state, 'revoke', approval_id=approval_id)
        assert state == before


def check_107(s):
    for job in ('report-4', 'report-5'):
        state = copy.deepcopy(s)
        before = copy.deepcopy(state)
        call(state, 'unassign', job=job)
        before['assignments'].pop(job)
        assert state == before
        call(state, 'unassign', job=job)
        assert state == before
        role = state['jobs'][job]['approved_role']
        call(state, 'assign', job=job, role=role)
        assert state['assignments'][job] == role
        other = 'report-5' if job == 'report-4' else 'report-4'
        assert state['assignments'][other] == s['assignments'][other]


def check_108(s):
    job = 'recover-9'
    name = s['jobs'][job]['account']
    old = s['consumers'][job]
    before = copy.deepcopy(s)
    current = call(s, 'renew', job=job)
    assert current != old and current == s['accounts'][name]['current']
    assert s['consumers'][job] == current
    assert s['credentials'][old]['revoked'] is True
    assert s['credentials'][current] == dict(account=name, expires_at=s['now'] + s['credential_ttl'], revoked=False)
    assert probe(s, job, 'model.recovery', 'generate') is True
    assert probe(s, job, 'model.recovery', 'generate', old) is False
    for account, spec in before['accounts'].items():
        if account != name:
            assert s['accounts'][account] == spec
    assert s['grants'] == before['grants']
    snapshot = copy.deepcopy(s)
    assert call(s, 'renew', job=job) == current
    assert s == snapshot
    try:
        call(s, 'renew', job='eval-12')
    except ValueError:
        pass
    else:
        raise AssertionError('inactive job renewed')
    assert s == snapshot
    # Hold out a shared account with two live consumers and one inactive consumer.
    held = copy.deepcopy(before)
    held['jobs']['recovery-sibling'] = dict(account=name, active=True)
    held['jobs']['recovery-retired'] = dict(account=name, active=False)
    held['consumers'].update({'recovery-sibling': old, 'recovery-retired': old})
    fresh = call(held, 'renew', job=job)
    assert held['consumers']['recovery-sibling'] == fresh
    assert held['consumers']['recovery-retired'] == old


def check_109(s):
    before = copy.deepcopy(s)
    call(s, 'retire')
    expected = {job: cred for job, cred in before['consumers'].items() if before['jobs'][job]['active']}
    before['consumers'] = expected
    assert s == before
    assert 'eval-17' in s['consumers'] and 'eval-12' not in s['consumers']
    call(s, 'retire')
    assert s == before
    s['jobs']['retired-sibling'] = dict(account='sa-report', active=False)
    s['consumers']['retired-sibling'] = s['consumers']['report-4']
    call(s, 'retire')
    assert 'retired-sibling' not in s['consumers']
    assert {'report-4', 'report-5'} <= s['consumers'].keys()


def check_110(s):
    s['now'] = s['review_epoch']
    s['grants'].append(dict(s['approvals']['eval-access'], approval_id='eval-access', revision=99))
    before = copy.deepcopy(s)
    findings = call(s, 'preview')
    assert s == before
    expected = []
    for grant in s['grants']:
        approval = s['approvals'].get(grant['approval_id'])
        job = s['jobs'].get(grant['job'])
        valid = bool(approval and job and job['active']
                     and approval['starts_at'] <= s['now'] < approval['expires_at']
                     and all(grant.get(k) == v for k, v in approval.items()))
        expected.append(dict(approval_id=grant['approval_id'], kept=valid))
    assert findings == expected
    assert call(s, 'preview') == expected and s == before
    assert call(s, 'review') == expected
    assert s['grants'] == [grant for grant, finding in zip(before['grants'], expected) if finding['kept']]


checks = [check_101, check_102, check_103, check_104, check_105,
          check_106, check_107, check_108, check_109, check_110]
results = {}
for ticket_id, check in zip(fixture['ticket_ids'], checks, strict=True):
    try:
        check(copy.deepcopy(fixture))
        results[ticket_id] = True
    except Exception:
        results[ticket_id] = False
print(json.dumps(results, sort_keys=True))

"""Skill + user-owned policy: default section, user override, proposals never auto-apply."""
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import conftest  # noqa: F401
import ccp_policy as P

SKILL = pathlib.Path(__file__).resolve().parents[1]/'skill'/'SKILL.md'


def test_default_policy_from_skill(tmp_path):
    pol = P.Policy(SKILL, tmp_path/'cfg')
    eff = pol.effective()
    assert not eff['user_policy_exists'] and 'default POLICY' in eff['source']
    assert any('dry run' in x.lower() for x in eff['rules']['pre_approved'])
    assert any('test message' in x.lower() for x in eff['rules']['never'])
    assert eff['rules']['surface_first']


def test_user_policy_overrides_and_merges(tmp_path):
    cfg = tmp_path/'cfg'; cfg.mkdir()
    (cfg/'policy.md').write_text('### Pre-approved\n- Everything in the docs Project.\n\n### Surface to the user first\n- Nothing else.\n')
    pol = P.Policy(SKILL, cfg)
    eff = pol.effective()
    assert eff['user_policy_exists'] and eff['rules']['pre_approved'] == ['Everything in the docs Project.']
    merged = pol.skill_with_policy()['text']
    assert 'Everything in the docs Project.' in merged and 'Replying "go ahead"' not in merged
    assert merged.count(P.BEGIN) == 1 and '## Etiquette' in merged


def test_propose_appends_without_applying(tmp_path):
    pol = P.Policy(SKILL, tmp_path/'cfg')
    r = pol.propose('Pre-approve Add folder cards for paths under the Project repo.', 'approved 3 times today', 'Scout')
    assert r['applied'] is False and pathlib.Path(r['proposals_path']).exists()
    pol.propose('Second idea')
    text = pathlib.Path(r['proposals_path']).read_text()
    assert text.count('## prop_') == 2 and 'Scout' in text
    assert not (tmp_path/'cfg'/'policy.md').exists()
    try: pol.propose('  '); assert False
    except ValueError: pass


def test_parse_policy_other_sections():
    r = P.parse_policy('### Pre-approved\n- a\n### Team notes\n- b\n')
    assert r['pre_approved'] == ['a'] and r['other'] == {'Team notes': ['b']}

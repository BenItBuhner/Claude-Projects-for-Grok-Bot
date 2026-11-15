"""Skill + user policy for agents that drive Claude Code Projects through this plugin.

The canonical skill ships in the repo at skill/SKILL.md. Its POLICY section (between the POLICY:BEGIN / POLICY:END
markers) is only the DEFAULT. The user owns the effective policy:

    <config dir>/policy.md            user-owned policy; when present it replaces the default POLICY section
    <config dir>/policy-proposals.md  agents append proposed changes here (claude_policy_propose); never auto-applied

Agents read both through MCP tools (claude_skill_get, claude_policy_get), so the skill, the policy, and the server's
tool list stay in sync without anyone copying files around.
"""
import re, hashlib, pathlib, datetime

BEGIN = '<!-- POLICY:BEGIN -->'
END = '<!-- POLICY:END -->'
SECTION_KEYS = [
    ('pre_approved', re.compile(r'pre-?approved', re.I)),
    ('surface_first', re.compile(r'surface|ask (the user|first)', re.I)),
    ('never', re.compile(r'^never', re.I)),
]


def _read(p):
    try: return pathlib.Path(p).read_text(encoding='utf-8')
    except Exception: return None


def default_policy(skill_text):
    if not skill_text or BEGIN not in skill_text or END not in skill_text: return ''
    return skill_text.split(BEGIN, 1)[1].split(END, 1)[0].strip('\n')


def parse_policy(text):
    """Markdown headings + bullet lists -> {pre_approved: [...], surface_first: [...], never: [...], other: {...}}."""
    out = {'pre_approved': [], 'surface_first': [], 'never': [], 'other': {}}
    cur = None; cur_title = None
    for line in (text or '').splitlines():
        h = re.match(r'^#{2,6}\s+(.*)$', line)
        if h:
            cur_title = h.group(1).strip(); cur = None
            for key, rx in SECTION_KEYS:
                if rx.search(cur_title): cur = key; break
            if cur is None: out['other'].setdefault(cur_title, [])
            continue
        b = re.match(r'^\s*[-*]\s+(.*)$', line)
        if b and cur_title is not None:
            item = b.group(1).strip()
            (out[cur] if cur else out['other'][cur_title]).append(item)
    return out


class Policy:
    def __init__(self, skill_path, config_dir):
        self.skill_path = pathlib.Path(skill_path)
        self.config_dir = pathlib.Path(config_dir)
        self.user_policy_path = self.config_dir/'policy.md'
        self.proposals_path = self.config_dir/'policy-proposals.md'

    def skill(self):
        txt = _read(self.skill_path) or ''
        return txt

    def effective(self):
        skill = self.skill()
        user = _read(self.user_policy_path)
        if user and user.strip():
            text, source = user.strip('\n'), str(self.user_policy_path)
        else:
            text, source = default_policy(skill), f'{self.skill_path} (default POLICY section)'
        return {'source': source, 'user_policy_path': str(self.user_policy_path), 'user_policy_exists': bool(user and user.strip()),
                'proposals_path': str(self.proposals_path), 'text': text, 'rules': parse_policy(text),
                'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}

    def skill_with_policy(self):
        skill = self.skill(); eff = self.effective()
        if BEGIN in skill and END in skill:
            pre, rest = skill.split(BEGIN, 1); _, post = rest.split(END, 1)
            merged = f'{pre}{BEGIN}\n{eff["text"]}\n{END}{post}'
        else:
            merged = skill + f'\n\n## POLICY\n{BEGIN}\n{eff["text"]}\n{END}\n'
        return {'skill_path': str(self.skill_path), 'policy_source': eff['source'], 'text': merged,
                'sha256': hashlib.sha256(merged.encode('utf-8')).hexdigest()[:16]}

    def propose(self, change, rationale='', proposed_by=''):
        change = (change or '').strip()
        if not change: raise ValueError('change is empty')
        if len(change) > 4000 or len(rationale or '') > 2000: raise ValueError('proposal too long (change <= 4000, rationale <= 2000 chars)')
        self.config_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        pid = 'prop_' + hashlib.sha1(f'{stamp}{change}'.encode()).hexdigest()[:8]
        entry = (f'\n## {pid} ({stamp}){(" by " + proposed_by.strip()[:60]) if proposed_by else ""}\n\n'
                 f'Status: proposed (the user applies it by editing {self.user_policy_path.name})\n\n'
                 f'Change:\n\n{change}\n\n' + (f'Why:\n\n{rationale.strip()}\n' if rationale else ''))
        new = not self.proposals_path.exists()
        with open(self.proposals_path, 'a', encoding='utf-8') as f:
            if new: f.write('# Policy proposals\n\nAgents append here. Nothing in this file takes effect until the user copies it into policy.md.\n')
            f.write(entry)
        return {'proposal_id': pid, 'proposals_path': str(self.proposals_path), 'applied': False,
                'note': f'Recorded only. The user decides; copy the change into {self.user_policy_path} to apply it.'}

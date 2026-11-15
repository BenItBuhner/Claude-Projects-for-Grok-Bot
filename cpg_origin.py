"""Message-origin bracket.

Every message this plugin types into a Claude Code thread (send and thread-create) starts with one header line:

    [sent by Grok Bot]
    [sent by Grok Bot · <bot name>]      (when a bot name is configured or passed)

Messages a person types into the Claude app themselves never carry it. Agents and threads can therefore tell
bot-sent requests from human ones by checking whether the first line starts with ORIGIN_PREFIX
(grep: '^\\[sent by Grok Bot'). The header is added exactly once: an existing header is never doubled.
"""
import re

ORIGIN_PREFIX = '[sent by Grok Bot'          # the single, greppable constant
ORIGIN_SEPARATOR = ' · '
ORIGIN_RE = re.compile(r'^\[sent by Grok Bot(?: · [^\]\n]{1,64})?\][ \t]*$')
_BOT_NAME_MAX = 40


def clean_bot_name(name):
    """Bot names are display text only: one line, no brackets, at most 40 characters."""
    if not name: return ''
    n = re.sub(r'[\[\]\r\n\t]+', ' ', str(name))
    n = re.sub(r'\s+', ' ', n).strip()
    return n[:_BOT_NAME_MAX].strip()


def origin_header(bot_name=None):
    n = clean_bot_name(bot_name)
    return f'{ORIGIN_PREFIX}{ORIGIN_SEPARATOR}{n}]' if n else f'{ORIGIN_PREFIX}]'


def has_origin(message):
    first = (message or '').lstrip('\ufeff').split('\n', 1)[0].rstrip('\r')
    return bool(ORIGIN_RE.match(first))


def add_origin(message, bot_name=None):
    """Prefix the header line unless the first line already is one (idempotent)."""
    message = message or ''
    if has_origin(message): return message
    return origin_header(bot_name) + '\n' + message


def strip_origin(message):
    """Inverse of add_origin, for comparing a read-back transcript with the text the caller asked to send."""
    message = message or ''
    if not has_origin(message): return message
    parts = message.split('\n', 1)
    return parts[1] if len(parts) > 1 else ''


_LEADING_RE = re.compile(r'^[\ufeff \t\r\n]*\[sent by Grok Bot(?: · [^\]\n]{1,64})?\][ \t]*(?:\r?\n)?')

# Attachment chips the app renders before the message text in a read-back transcript: "[attached image: x.png]",
# "[MP4 ember-dots.mp4]", "[x.png]", or a bare badge + file name ("PNG x.png"). Only a run of these, directly
# followed by the bracket, is skipped; a bracket anywhere later in the text still doesn't count.
_CHIP = r'(?:\[(?!sent by Grok Bot)[^\]\n]{1,240}\]|(?:[A-Z0-9]{2,5} )?[^\s\[\]]{1,200}\.[A-Za-z0-9]{1,10})'
_CHIPS_RE = re.compile(r'^[\ufeff \t\r\n]*(?:' + _CHIP + r'[ \t\r\n]*)+(?=\[sent by Grok Bot)')


def _skip_chips(text):
    m = _CHIPS_RE.match(text or '')
    return (text or '')[m.end():] if m else (text or '')


def origin_body(text):
    """Text with a leading origin bracket removed, also when the read-back collapsed the header's newline
    ('[sent by Grok Bot] body'). Text without a bracket is returned unchanged. Use it for summaries and
    for comparing a bot-sent message with the words the caller asked to send."""
    text = text or ''
    m = _LEADING_RE.match(text) or _LEADING_RE.match(_skip_chips(text))
    if not m: return text
    return (text if _LEADING_RE.match(text) else _skip_chips(text))[m.end():].lstrip(' \t')


def is_bot_sent(text):
    """Lenient check for transcripts read back from the app (which may collapse the header's newline):
    a message is bot-sent when its text starts with ORIGIN_PREFIX, also after attachment chips the app shows first."""
    return _skip_chips(text).lstrip('\ufeff \t\r\n').startswith(ORIGIN_PREFIX)

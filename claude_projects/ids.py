"""Shapes of the ids this plugin hands out, shared by the CLI and the MCP server (which does not import core)."""
import re

from claude_projects.origin import strip_origin

JOB_ID_RE = re.compile(r'^s[0-9a-f]{23}$')


def is_job_id(message):
    """The text (origin bracket aside) is nothing but a send job id."""
    return bool(JOB_ID_RE.match(strip_origin(message or '').strip()))

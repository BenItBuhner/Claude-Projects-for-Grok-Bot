"""`python -m claude_projects ...`: the claude-projects CLI. `claude-projects [--json] setup ...` is the guided setup
flow (standard library only, so it also runs before install.sh); everything else is the Projects tool itself."""
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    for a in argv:
        if a.startswith('--'): continue
        if a == 'setup':
            from claude_projects import setup as setup_mod
            return setup_mod.main(argv)
        break
    from claude_projects import core as core_mod
    return core_mod.main(argv)


if __name__ == '__main__':
    sys.exit(main())

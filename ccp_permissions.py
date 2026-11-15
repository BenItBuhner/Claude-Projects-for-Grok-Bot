"""Old name of cpg_permissions.py, kept so existing units, scripts and imports keep working (renamed in 1.0.2).

Importing ccp_permissions gives the cpg_permissions module itself (same object, so patches and globals are shared). Running this file
(python ccp_permissions.py ...) runs cpg_permissions exactly as `python cpg_permissions.py ...` would, so the process command line still
says ccp_permissions.py for tools that look for it.
"""
import sys
if __name__ == '__main__':
    import runpy
    runpy.run_module('cpg_permissions', run_name='__main__', alter_sys=True)
else:
    import importlib
    sys.modules[__name__] = importlib.import_module('cpg_permissions')

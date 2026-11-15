"""Old name of cpg_policy.py, kept so existing units, scripts and imports keep working (renamed in 1.0.2).

Importing ccp_policy gives the cpg_policy module itself (same object, so patches and globals are shared). Running this file
(python ccp_policy.py ...) runs cpg_policy exactly as `python cpg_policy.py ...` would, so the process command line still
says ccp_policy.py for tools that look for it.
"""
import sys
if __name__ == '__main__':
    import runpy
    runpy.run_module('cpg_policy', run_name='__main__', alter_sys=True)
else:
    import importlib
    sys.modules[__name__] = importlib.import_module('cpg_policy')

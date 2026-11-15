"""Old name of cpg_core.py, kept so existing units, scripts and imports keep working (renamed in 1.0.2).

Importing ccp_core gives the cpg_core module itself (same object, so patches and globals are shared). Running this file
(python ccp_core.py ...) runs cpg_core exactly as `python cpg_core.py ...` would, so the process command line still
says ccp_core.py for tools that look for it.
"""
import sys
if __name__ == '__main__':
    import runpy
    runpy.run_module('cpg_core', run_name='__main__', alter_sys=True)
else:
    import importlib
    sys.modules[__name__] = importlib.import_module('cpg_core')

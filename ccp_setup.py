"""Old name of cpg_setup.py, kept so existing units, scripts and imports keep working (renamed in 1.0.2).

Importing ccp_setup gives the cpg_setup module itself (same object, so patches and globals are shared). Running this file
(python ccp_setup.py ...) runs cpg_setup exactly as `python cpg_setup.py ...` would, so the process command line still
says ccp_setup.py for tools that look for it.
"""
import sys
if __name__ == '__main__':
    import runpy
    runpy.run_module('cpg_setup', run_name='__main__', alter_sys=True)
else:
    import importlib
    sys.modules[__name__] = importlib.import_module('cpg_setup')

"""Old name of cpg_origin.py, kept so existing units, scripts and imports keep working (renamed in 1.0.2).

Importing ccp_origin gives the cpg_origin module itself (same object, so patches and globals are shared). Running this file
(python ccp_origin.py ...) runs cpg_origin exactly as `python cpg_origin.py ...` would, so the process command line still
says ccp_origin.py for tools that look for it.
"""
import sys
if __name__ == '__main__':
    import runpy
    runpy.run_module('cpg_origin', run_name='__main__', alter_sys=True)
else:
    import importlib
    sys.modules[__name__] = importlib.import_module('cpg_origin')

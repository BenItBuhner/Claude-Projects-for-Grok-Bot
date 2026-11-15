"""Old name of cpg_idb.py, kept so existing units, scripts and imports keep working (renamed in 1.0.2).

Importing ccp_idb gives the cpg_idb module itself (same object, so patches and globals are shared). Running this file
(python ccp_idb.py ...) runs cpg_idb exactly as `python cpg_idb.py ...` would, so the process command line still
says ccp_idb.py for tools that look for it.
"""
import sys
if __name__ == '__main__':
    import runpy
    runpy.run_module('cpg_idb', run_name='__main__', alter_sys=True)
else:
    import importlib
    sys.modules[__name__] = importlib.import_module('cpg_idb')

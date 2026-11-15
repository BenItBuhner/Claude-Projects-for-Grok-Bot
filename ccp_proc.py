"""Old name of cpg_proc.py, kept so existing units, scripts and imports keep working (renamed in 1.0.2).

Importing ccp_proc gives the cpg_proc module itself (same object, so patches and globals are shared). Running this file
(python ccp_proc.py ...) runs cpg_proc exactly as `python cpg_proc.py ...` would, so the process command line still
says ccp_proc.py for tools that look for it.
"""
import sys
if __name__ == '__main__':
    import runpy
    runpy.run_module('cpg_proc', run_name='__main__', alter_sys=True)
else:
    import importlib
    sys.modules[__name__] = importlib.import_module('cpg_proc')

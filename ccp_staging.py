"""Old name of cpg_staging.py, kept so existing units, scripts and imports keep working (renamed in 1.0.2).

Importing ccp_staging gives the cpg_staging module itself (same object, so patches and globals are shared). Running this file
(python ccp_staging.py ...) runs cpg_staging exactly as `python cpg_staging.py ...` would, so the process command line still
says ccp_staging.py for tools that look for it.
"""
import sys
if __name__ == '__main__':
    import runpy
    runpy.run_module('cpg_staging', run_name='__main__', alter_sys=True)
else:
    import importlib
    sys.modules[__name__] = importlib.import_module('cpg_staging')

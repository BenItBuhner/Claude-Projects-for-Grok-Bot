"""Old name of cpg_config.py, kept so existing units, scripts and imports keep working (renamed in 1.0.2).

Importing ccp_config gives the cpg_config module itself (same object, so patches and globals are shared). Running this file
(python ccp_config.py ...) runs cpg_config exactly as `python cpg_config.py ...` would, so the process command line still
says ccp_config.py for tools that look for it.
"""
import sys
if __name__ == '__main__':
    import runpy
    runpy.run_module('cpg_config', run_name='__main__', alter_sys=True)
else:
    import importlib
    sys.modules[__name__] = importlib.import_module('cpg_config')

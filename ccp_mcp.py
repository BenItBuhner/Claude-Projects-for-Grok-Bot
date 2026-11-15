"""Old name of cpg_mcp.py, kept so existing units, scripts and imports keep working (renamed in 1.0.2).

Importing ccp_mcp gives the cpg_mcp module itself (same object, so patches and globals are shared). Running this file
(python ccp_mcp.py ...) runs cpg_mcp exactly as `python cpg_mcp.py ...` would, so the process command line still
says ccp_mcp.py for tools that look for it.
"""
import sys
if __name__ == '__main__':
    import runpy
    runpy.run_module('cpg_mcp', run_name='__main__', alter_sys=True)
else:
    import importlib
    sys.modules[__name__] = importlib.import_module('cpg_mcp')

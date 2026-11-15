Raw `wsl.exe` output used by tests/test_host_ref.py: the old inbox wsl.exe prints UTF-16LE (with or
without a BOM) and ignores `WSL_UTF8`; a current one prints UTF-8 with `WSL_UTF8=1`.

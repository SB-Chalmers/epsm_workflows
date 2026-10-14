"""Compatibility import for a historical study now owned by epsm_studies."""
from epsm_workflows._historical import load as _load
_historical_module = _load('epsm_studies.legacy_workflows.mf_emulator')
if __name__ == "__main__":
    _historical_module.main()
else:
    import sys as _sys
    _sys.modules[__name__] = _historical_module

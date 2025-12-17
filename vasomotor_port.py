"""
Compatibility shim that re-exports the VasoMoto serial helper.

The implementation now lives in `vasotracker_2.vasomotor_port` so that the
application package can import it without relying on repository-relative
paths.  Existing scripts that import `vasomotor_port.VasoMotoPort` continue
to work via this forwarding module.
"""

from vasotracker_2.vasomotor_port import VasoMotoPort

__all__ = ["VasoMotoPort"]

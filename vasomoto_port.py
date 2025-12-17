"""
Compatibility shim that re-exports the VasoMoto serial helper.

The implementation now lives in `vasotracker_2.vasomoto_port` so that the
application package can import it without relying on repository-relative
paths.  Existing scripts that import `vasomoto_port.VasoMotoPort` continue
to work via this forwarding module.
"""

from vasotracker_2.vasomoto_port import VasoMotoPort

__all__ = ["VasoMotoPort"]

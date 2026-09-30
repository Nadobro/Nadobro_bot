"""Arcus venue library (Arcus perps as Nadobro's second venue).

Pure library, unwired: nothing outside this package imports it yet. Modules:

- ``types``   — value types, constants, clientId format helpers (no I/O).
- ``errors``  — typed call outcomes (``Ok``/``Accepted``/``Rejected``/…) and
  ``classify_http``; DENIED is never EMPTY.
- ``signing`` — Ed25519 signer, both Arcus signing schemes, canonical JSON,
  exact Decimal -> integer tick/quantum conversion.
- ``clock``   — ``/v1/time`` offset and strictly increasing per-key ``ct``.
- ``parse``   — tolerant, fail-closed response parsers + small view types.

This file deliberately imports nothing, so ``import …venue.arcus.types`` never
drags in an HTTP client or a crypto backend.
"""

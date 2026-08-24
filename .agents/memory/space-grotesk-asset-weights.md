---
name: Space Grotesk asset weights
description: How the bundled PnL-card font paths map to Space Grotesk’s published static faces.
---

Space Grotesk’s upstream static distribution provides Regular, Medium, and Bold, but not a distinct SemiBold file. Keep the renderer’s expected semibold path supplied by the official Medium face unless the brand provides a true semibold asset.

**Why:** The card renderer needs stable, bundled font paths in CI and production, while substituting a different font would visibly change the brand typography.

**How to apply:** When refreshing the PnL-card font assets, retain Regular, Medium, and Bold from the official Space Grotesk distribution and use Medium for the expected semibold filename until an approved 600-weight file is available.
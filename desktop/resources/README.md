# Packaging resources

electron-builder reads this directory (`directories.buildResources`).

- `icon.png` — the app icon, 1024x1024 with transparent corners, rendered by
  `scripts/make-icon.py` from the AgentOS mark's geometry (the repo only has
  a 234 px PNG of the mark, so it is redrawn as vectors and downsampled).
  electron-builder derives the `.icns`; `main/index.ts` also sets it as the
  Dock icon for `electron-vite dev`, where the bundle is Electron's.
- `entitlements.mac.plist` — Hardened Runtime exceptions Electron needs
  (JIT, unsigned executable memory); required for Developer ID + notarization.

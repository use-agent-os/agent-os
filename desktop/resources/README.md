# Packaging resources

electron-builder reads this directory (`directories.buildResources`).

- `icon.png` — the app icon, 1024x1024 with transparent corners, rendered by
  `scripts/make-icon.py` from the AgentOS mark's geometry (the repo only has
  a 234 px PNG of the mark, so it is redrawn as vectors and downsampled).
  electron-builder derives the `.icns`; `main/index.ts` also sets it as the
  Dock icon for `electron-vite dev`, where the bundle is Electron's.
- `trayTemplate.png` / `trayTemplate@2x.png` — the menu bar icon (18x18 and
  36x36), the same mark black on transparent with thicker spokes, rendered
  by `scripts/make-tray-icon.py`. The `Template` suffix makes macOS tint it
  for a light or dark menu bar. Shipped through `extraResources`, since
  nothing in this directory reaches the packaged app by itself.
- `entitlements.mac.plist` — Hardened Runtime exceptions Electron needs
  (JIT, unsigned executable memory, DYLD environment variables, disabled
  library validation); required for Developer ID + notarization.
- `pets/` — the pets that ship with the app, one petdex folder each
  (`pet.json` + `spritesheet.webp`; today only `agentos`, Luma). Shipped
  through `extraResources`; `PetStore.seedBundled` copies them into the pets
  directory on first launch.

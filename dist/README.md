# dist/

Release staging for Atelier Rust binaries.

```bash
bash scripts/build-release.sh
# → dist/bin/atelier-server
# → dist/bin/atelier-cli

bash install.sh
# copies binaries to ~/.local/bin and links `atelier` CLI
```

Do not commit large binaries unless packaging a release asset.

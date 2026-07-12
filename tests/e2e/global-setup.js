import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const RUST_MANIFEST = path.join(REPO, 'rust', 'Cargo.toml');

export default function globalSetup() {
  execFileSync(
    'cargo',
    [
      'build',
      '--manifest-path',
      RUST_MANIFEST,
      '-p',
      'atelier-cli',
      '-p',
      'atelier-server',
      '-p',
      'atelier-daemon',
      '-p',
      'atelier-mcp',
    ],
    { cwd: REPO, stdio: 'pipe' }
  );
}

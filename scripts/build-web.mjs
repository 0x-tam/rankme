import { cp, lstat, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';

const root = resolve(import.meta.dirname, '..');
const output = resolve(root, 'dist/public');
const assets = ['index.html', 'app.js', 'auth.js', 'styles.css', 'favicon.svg'];

for (const asset of assets) {
  const info = await lstat(resolve(root, 'static', asset));
  if (!info.isFile() || info.nlink !== 1) throw Error(`Invalid public asset: ${asset}`);
}

await rm(output, { recursive: true, force: true });
await mkdir(resolve(output, 'static'), { recursive: true });
const index = await readFile(resolve(root, 'static/index.html'), 'utf8');
await writeFile(resolve(output, 'index.html'), index.replace('<html lang="en">', '<html lang="en" data-rankme-hosted="true">'));
for (const asset of assets.filter(name => name !== 'index.html')) {
  await cp(resolve(root, 'static', asset), resolve(output, 'static', asset));
}
const emitted = await readdir(output, { recursive: true });
const expected = new Set(['index.html', 'static', ...assets.filter(name => name !== 'index.html').map(name => `static/${name}`)]);
if (emitted.some(name => !expected.has(name))) throw Error('Unexpected file in public build output');
console.log('Built public RankMe assets.');

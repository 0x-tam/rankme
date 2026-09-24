import { cp, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';

const root = resolve(import.meta.dirname, '..');
const stage = resolve(root, '.vercel-stage');
const output = resolve(root, 'dist/public');
const assets = ['index.html', 'static/app.js', 'static/auth.js', 'static/styles.css', 'static/favicon.svg'];

// Rebuild and seal the exact public artifact before a deploy command can read it.
await import('./build-web.mjs');
const link = JSON.parse(await readFile(resolve(root, '.vercel/project.json'), 'utf8'));
if (!/^prj_[A-Za-z0-9]+$/.test(link.projectId || '') || !/^team_[A-Za-z0-9]+$/.test(link.orgId || '')) {
  throw Error('Link this checkout to the intended Vercel project before packaging.');
}
const sourceConfig = JSON.parse(await readFile(resolve(root, 'vercel.json'), 'utf8'));
if (sourceConfig.rewrites?.length !== 1 || sourceConfig.rewrites[0].source !== '/api/:path*' ||
    !sourceConfig.rewrites[0].destination?.startsWith('https://')) {
  throw Error('Unexpected API rewrite in Vercel configuration.');
}
await rm(stage, { recursive: true, force: true });
await mkdir(resolve(stage, '.vercel'), { recursive: true });
await mkdir(resolve(stage, 'public/static'), { recursive: true });
await writeFile(resolve(stage, '.vercel/project.json'), JSON.stringify({
  projectId: link.projectId, orgId: link.orgId, projectName: link.projectName,
}));
await writeFile(resolve(stage, 'vercel.json'), JSON.stringify({
  ...sourceConfig,
  framework: null,
  buildCommand: '',
  installCommand: '',
  outputDirectory: 'public',
}, null, 2) + '\n');
for (const asset of assets) await cp(resolve(output, asset), resolve(stage, 'public', asset));

const actual = (await readdir(stage, { recursive: true })).filter(name =>
  !['.vercel', 'public', 'public/static'].includes(name)).sort();
const expected = ['.vercel/project.json', 'vercel.json', ...assets.map(name => `public/${name}`)].sort();
if (JSON.stringify(actual) !== JSON.stringify(expected)) throw Error('Unexpected file in Vercel stage.');
console.log('Prepared isolated Vercel stage with only public assets, routing configuration, and project link.');

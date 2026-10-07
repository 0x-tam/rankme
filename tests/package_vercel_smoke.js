'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const publicRoot = path.resolve(__dirname, '../.vercel-stage/public');
const html = fs.readFileSync(path.join(publicRoot, 'index.html'), 'utf8');
const references = [...html.matchAll(/(?:src|href)=["'](\/static\/[^"']+)["']/g)]
  .map(match => match[1]);
assert(references.includes('/static/seo-features.js'), 'SEO workspace script must be referenced');
for (const reference of references) {
  const asset = path.join(publicRoot, reference.slice(1));
  assert(fs.statSync(asset).isFile(), `${reference} must be included in the Vercel package`);
}
console.log(`Vercel package contains all ${references.length} referenced static assets.`);

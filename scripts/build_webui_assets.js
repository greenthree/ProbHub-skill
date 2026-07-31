const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const repoRoot = path.resolve(__dirname, '..');
const webuiRoot = path.join(repoRoot, 'scripts', 'webui');
const vendorRoot = path.join(webuiRoot, 'vendor');
const fontRoot = path.join(vendorRoot, 'fonts');
const licenseRoot = path.join(vendorRoot, 'licenses');
const manifestPath = path.join(webuiRoot, 'asset-manifest.json');
const textAssetExtensions = new Set(['.cjs', '.css', '.html', '.js', '.json', '.txt']);

const packageVersions = {
  '@alpinejs/collapse': '3.15.12',
  '@alpinejs/csp': '3.15.12',
  '@fontsource-variable/ibm-plex-sans': '5.3.0',
  '@fontsource-variable/jetbrains-mono': '5.3.0',
  '@fontsource-variable/noto-serif-sc': '5.3.0',
  '@tailwindcss/typography': '0.5.20',
  marked: '18.0.7',
  'mathjax-full': '3.2.2',
  sortablejs: '1.15.7',
  tailwindcss: '3.4.19',
};

function packageRoot(name) {
  return path.join(repoRoot, 'node_modules', ...name.split('/'));
}

function verifyPackage(name) {
  const root = packageRoot(name);
  const metadata = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
  const expected = packageVersions[name];
  if (metadata.version !== expected) {
    throw new Error(`${name}: expected ${expected}, found ${metadata.version}`);
  }
  return root;
}

function copyPackageFile(name, source, destination) {
  const root = verifyPackage(name);
  fs.copyFileSync(path.join(root, source), path.join(vendorRoot, destination));
}

function copyLicense(name, source, destination) {
  const root = verifyPackage(name);
  fs.copyFileSync(path.join(root, source), path.join(licenseRoot, destination));
}

function listFiles(root, current = root) {
  const files = [];
  for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
    const absolute = path.join(current, entry.name);
    if (entry.isDirectory()) files.push(...listFiles(root, absolute));
    else if (entry.isFile()) files.push(path.relative(root, absolute).replaceAll('\\', '/'));
  }
  return files;
}

function writeManifest() {
  const runtimeFiles = [
    'index.html',
    'app.css',
    'app.js',
    'mathjax-config.js',
    'theme.js',
    ...listFiles(vendorRoot).map((relative) => `vendor/${relative}`),
  ].sort();
  const files = {};
  for (const relative of runtimeFiles) {
    const absolute = path.resolve(webuiRoot, relative);
    if (!absolute.startsWith(path.resolve(webuiRoot) + path.sep)) {
      throw new Error(`manifest path escaped scripts/webui: ${relative}`);
    }
    let data = fs.readFileSync(absolute);
    let algorithm = 'sha256';
    if (textAssetExtensions.has(path.extname(relative).toLowerCase())) {
      data = Buffer.from(data.toString('utf8').replace(/\r\n?/g, '\n'), 'utf8');
      algorithm = 'sha256-text-lf';
    }
    files[relative] = `${algorithm}:${crypto.createHash('sha256').update(data).digest('hex')}`;
  }
  fs.writeFileSync(manifestPath, `${JSON.stringify({ schema_version: 1, files }, null, 2)}\n`, 'utf8');
}

if (process.argv.slice(2).includes('--manifest')) {
  writeManifest();
  process.exit(0);
}

const resolvedVendor = path.resolve(vendorRoot);
if (!resolvedVendor.startsWith(path.resolve(webuiRoot) + path.sep)) {
  throw new Error('vendor output escaped scripts/webui');
}
fs.rmSync(vendorRoot, { recursive: true, force: true });
fs.mkdirSync(fontRoot, { recursive: true });
fs.mkdirSync(licenseRoot, { recursive: true });

copyPackageFile('@alpinejs/collapse', 'dist/cdn.min.js', 'alpine-collapse.js');
copyPackageFile('@alpinejs/csp', 'dist/cdn.min.js', 'alpine.js');
copyPackageFile('marked', 'lib/marked.umd.js', 'marked.js');
copyPackageFile('mathjax-full', 'es5/tex-svg.js', 'mathjax-tex-svg.js');
copyPackageFile('sortablejs', 'Sortable.min.js', 'sortable.js');

const fontPackages = [
  ['@fontsource-variable/ibm-plex-sans', 'IBM Plex Sans Variable', 'IBM Plex Sans'],
  ['@fontsource-variable/jetbrains-mono', 'JetBrains Mono Variable', 'JetBrains Mono'],
  ['@fontsource-variable/noto-serif-sc', 'Noto Serif SC Variable', 'Noto Serif SC'],
];
const fontCss = [];
for (const [name, sourceFamily, targetFamily] of fontPackages) {
  const root = verifyPackage(name);
  let css = fs.readFileSync(path.join(root, 'wght.css'), 'utf8');
  const files = [...css.matchAll(/url\(([^)]+)\)/g)].map((match) =>
    match[1].replace(/^['"]|['"]$/g, '')
  );
  for (const relative of new Set(files)) {
    const source = path.resolve(root, relative);
    const filesRoot = path.resolve(root, 'files') + path.sep;
    if (!source.startsWith(filesRoot) || path.extname(source) !== '.woff2') {
      throw new Error(`${name}: unsafe font reference ${relative}`);
    }
    fs.copyFileSync(source, path.join(fontRoot, path.basename(source)));
  }
  css = css
    .replaceAll(`font-family: '${sourceFamily}'`, `font-family: '${targetFamily}'`)
    .replaceAll('url(./files/', 'url(./fonts/');
  fontCss.push(`/* ${name}@${packageVersions[name]} */\n${css.trim()}`);
}
fs.writeFileSync(path.join(vendorRoot, 'fonts.css'), `${fontCss.join('\n\n')}\n`, 'utf8');

copyLicense('marked', 'LICENSE', 'marked.txt');
copyLicense('mathjax-full', 'LICENSE', 'mathjax-full.txt');
copyLicense('sortablejs', 'LICENSE', 'sortablejs.txt');
copyLicense('tailwindcss', 'LICENSE', 'tailwindcss.txt');
copyLicense('@tailwindcss/typography', 'LICENSE', 'tailwindcss-typography.txt');
copyLicense('@fontsource-variable/ibm-plex-sans', 'LICENSE', 'ibm-plex-sans.txt');
copyLicense('@fontsource-variable/jetbrains-mono', 'LICENSE', 'jetbrains-mono.txt');
copyLicense('@fontsource-variable/noto-serif-sc', 'LICENSE', 'noto-serif-sc.txt');

const alpineLicense = `MIT License

Copyright (c) 2019 Caleb Porzio

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
`;
fs.writeFileSync(path.join(licenseRoot, 'alpinejs.txt'), alpineLicense, 'utf8');

const notices = Object.entries(packageVersions)
  .map(([name, version]) => `${name}@${version}`)
  .join('\n');
fs.writeFileSync(
  path.join(vendorRoot, 'THIRD_PARTY_NOTICES.txt'),
  `ProbHub WebUI bundled dependencies\n\n${notices}\n\nFull license texts are in ./licenses/.\n`,
  'utf8'
);

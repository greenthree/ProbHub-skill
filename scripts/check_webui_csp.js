const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const htmlPath = path.join(root, 'scripts', 'webui', 'index.html');
const parserPath = path.join(
  path.dirname(require.resolve('@alpinejs/csp/package.json')),
  'src',
  'parser.js'
);

function lineAt(text, offset) {
  return text.slice(0, offset).split('\n').length;
}

async function main() {
  const parserSource = fs.readFileSync(parserPath, 'utf8');
  const parserModule = await import(
    `data:text/javascript;base64,${Buffer.from(parserSource).toString('base64')}`
  );
  const html = fs.readFileSync(htmlPath, 'utf8');
  const expressionAttribute =
    /(?:^|\s)(x-(?:data|init|show|if|text|html|model|effect|id)(?:\.[^\s=>]+)*|[@:][^\s=>]+)\s*=\s*"([^"]*)"/gm;
  const errors = [];
  let checked = 0;
  let match;
  while ((match = expressionAttribute.exec(html)) !== null) {
    const [, name, expression] = match;
    if (!expression.trim()) continue;
    checked += 1;
    try {
      const tokens = new parserModule.Tokenizer(expression).tokenize();
      new parserModule.Parser(tokens).parse();
    } catch (error) {
      errors.push(`${htmlPath}:${lineAt(html, match.index)} ${name}: ${error.message}`);
    }
  }
  if (!checked) throw new Error('no Alpine CSP expressions found');
  if (errors.length) throw new Error(`Alpine CSP expression check failed:\n${errors.join('\n')}`);
  process.stdout.write(`Alpine CSP expressions: ${checked} parsed\n`);
}

main().catch((error) => {
  console.error(error.message || error);
  process.exitCode = 1;
});

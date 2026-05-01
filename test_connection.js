const https = require('https');
const crypto = require('crypto');
const fs = require('fs');

// Load credentials from code.env
const envContent = fs.readFileSync('./code.env', 'utf8');
const env = {};
envContent.split('\n').forEach(line => {
  const [key, ...val] = line.split('=');
  if (key && val.length) env[key.trim()] = val.join('=').trim().replace(/\\n/g, '\n');
});

const API_KEY = env.API_KEY;
const SECRET_KEY = env.SECRET_KEY;

function buildJWT(method, path) {
  const now = Math.floor(Date.now() / 1000);
  const header = Buffer.from(JSON.stringify({ alg: 'ES256', kid: API_KEY })).toString('base64url');
  const payload = Buffer.from(JSON.stringify({
    sub: API_KEY,
    iss: 'coinbase-cloud',
    nbf: now,
    exp: now + 120,
    iat: now,
    uri: `${method} api.coinbase.com${path}`
  })).toString('base64url');

  const signingInput = `${header}.${payload}`;
  const sign = crypto.createSign('SHA256');
  sign.update(signingInput);
  const signature = sign.sign({ key: SECRET_KEY, dsaEncoding: 'ieee-p1363' }).toString('base64url');

  return `${signingInput}.${signature}`;
}

function apiRequest(path) {
  return new Promise((resolve, reject) => {
    const jwt = buildJWT('GET', path);
    const options = {
      hostname: 'api.coinbase.com',
      path,
      method: 'GET',
      headers: { Authorization: `Bearer ${jwt}`, 'Content-Type': 'application/json' }
    };
    const req = https.request(options, res => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => resolve(JSON.parse(data)));
    });
    req.on('error', reject);
    req.end();
  });
}

async function main() {
  console.log('Testando conexão com Coinbase...\n');

  try {
    const result = await apiRequest('/api/v3/brokerage/accounts');

    if (result.accounts) {
      console.log('Conexão bem-sucedida!\n');
      console.log('=== SALDO E CRIPTOATIVOS ===\n');

      const accounts = result.accounts.filter(a => parseFloat(a.available_balance?.value || 0) > 0);

      if (accounts.length === 0) {
        console.log('Nenhum saldo encontrado.');
        console.log('\nTodas as contas:');
        result.accounts.slice(0, 5).forEach(a => {
          console.log(`  ${a.currency}: ${a.available_balance?.value || 0}`);
        });
      } else {
        accounts.forEach(a => {
          console.log(`${a.currency}: ${parseFloat(a.available_balance?.value).toFixed(8)}`);
        });
      }
    } else {
      console.log('Resposta inesperada:', JSON.stringify(result, null, 2));
    }
  } catch (err) {
    console.error('Erro:', err.message);
  }
}

main();

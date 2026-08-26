#!/usr/bin/env node
// Read-only Jupiter swap quote -- no key needed, no funds move.
// Usage: node wallet/quote.mjs <inputMint> <outputMint> <amountBaseUnits> [slippageBps]
const [, , inputMint, outputMint, amount, slippageBps] = process.argv;

if (!inputMint || !outputMint || !amount) {
  console.error("Usage: node wallet/quote.mjs <inputMint> <outputMint> <amountBaseUnits> [slippageBps]");
  process.exit(1);
}

async function main() {
  // https://api.jup.ag/swap/v1 -- the old quote-api.jup.ag/v6 host and the
  // intermediate lite-api.jup.ag host are both deprecated (lite-api.jup.ag
  // as of 2026-01-31); confirmed live 2026-08-21 that quote-api.jup.ag no
  // longer even resolves in DNS, while this endpoint works.
  const url =
    `https://api.jup.ag/swap/v1/quote?inputMint=${inputMint}&outputMint=${outputMint}` +
    `&amount=${amount}&slippageBps=${slippageBps || 100}`;
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`Jupiter quote request failed: HTTP ${res.status} ${await res.text()}`);
  }
  console.log(JSON.stringify(await res.json(), null, 2));
}

main().catch((err) => {
  console.error(`Error: ${err.message}`);
  process.exit(1);
});

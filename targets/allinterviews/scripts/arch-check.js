#!/usr/bin/env node
/**
 * Architectural boundary checker.
 * Add checks here via Review Feedback Promotion (see CONSTRAINTS.md).
 * A check is only added when a violation pattern is confirmed real — not speculatively.
 */

const { execSync } = require('child_process');
const path = require('path');

const ROOT = path.resolve(__dirname, '../..');
let failed = false;

function grep(pattern, dirs, flags = '') {
  try {
    const result = execSync(
      `grep -rn ${flags} "${pattern}" ${dirs.join(' ')}`,
      { cwd: ROOT, stdio: ['pipe', 'pipe', 'pipe'] }
    ).toString().trim();
    return result ? result.split('\n') : [];
  } catch {
    return [];
  }
}

function fail(message, matches) {
  failed = true;
  console.error(`\n[arch-check] FAIL: ${message}`);
  matches.forEach(m => console.error(`  ${m}`));
}

// ── Check 1 ──────────────────────────────────────────────────────────────────
// process.exit() must only appear in server.js and config/index.js.
// Calling it elsewhere causes silent server death with no cleanup or error log.
{
  const matches = grep('process\\.exit', ['routes/', 'constants/', 'models/']);
  if (matches.length > 0) {
    fail(
      'process.exit() called outside server.js / config/index.js.\n' +
      '  Why banned: exits the process with no cleanup or error logging.\n' +
      '  Fix: throw an Error instead and let the Express error handler respond.',
      matches
    );
  }
}

// ── Check 2 ──────────────────────────────────────────────────────────────────
// classifyFields() must always be called with response_format option.
// Without it, OpenAI can return plain text instead of JSON, crashing the route.
// Production incident: JSON.parse threw on non-JSON response (see DECISIONS.md D6).
{
  const matches = grep('classifyFields', ['routes/', 'constants/']);
  const violations = matches.filter(line => {
    // Flag lines that call classifyFields but are not the definition itself
    // and are not in a file that contains response_format nearby.
    // Simple heuristic: flag any classifyFields( call site.
    // The actual enforcement is that the implementation file must contain
    // response_format — checked separately below.
    return false; // placeholder — see check 2b below
  });

  // Check 2b: the classifyFields implementation must contain response_format.
  const implFiles = grep('response_format', ['constants/prankCheckerOpenAI.js']);
  if (implFiles.length === 0) {
    fail(
      'classifyFields() implementation is missing response_format: { type: "json_object" }.\n' +
      '  Why required: prevents OpenAI returning plain text instead of JSON (production incident).\n' +
      '  Fix: add response_format: { type: "json_object" } to the OpenAI API call options.',
      ['constants/prankCheckerOpenAI.js']
    );
  }
}

// ─────────────────────────────────────────────────────────────────────────────

if (failed) {
  process.exit(1);
} else {
  console.log('[arch-check] OK');
}

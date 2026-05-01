# Patch: config/index.js

## Change
Add "test" to the valid NODE_ENV values.

## Location
Line: `assertInValues("NODE_ENV", ["production", "development", "local"]);`

## Replace with
`assertInValues("NODE_ENV", ["production", "development", "local", "test"]);`

## Why
Without this, `NODE_ENV=test jest` exits immediately with:
"Config key invalid: NODE_ENV, not in ["production","development","local"]"

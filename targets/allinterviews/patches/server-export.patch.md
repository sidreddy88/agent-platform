# Patch: server.js

## Change
Export the Express app and only call app.listen() when run directly (not when required by tests).

## Location
At the bottom of server.js, replace:

```js
const port = config.get('port');
app.listen(port, () => {
  addIPToWhitelist().then(() => {}).catch(() => {});
  console.log(`Server up and running on port ${port} !`)
});
```

## Replace with

```js
const port = config.get('port');
if (require.main === module) {
  app.listen(port, () => {
    addIPToWhitelist().then(() => {}).catch(() => {});
    console.log(`Server up and running on port ${port} !`);
  });
}

module.exports = app;
```

## Why
supertest needs to import the Express app without it calling app.listen().
`require.main === module` is true only when node runs server.js directly,
not when it is required by a test file.

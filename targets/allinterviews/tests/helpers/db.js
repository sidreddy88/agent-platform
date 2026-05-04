const mongoose = require('mongoose');

let mongod;

async function connect() {
  if (process.env.MONGO_URI) {
    // Docker environment: connect to real MongoDB container.
    // Retry for up to 30s — the mongo container may not be accepting connections yet.
    const deadline = Date.now() + 30_000;
    while (true) {
      try {
        await mongoose.connect(process.env.MONGO_URI, { serverSelectionTimeoutMS: 3000 });
        break;
      } catch (err) {
        if (Date.now() >= deadline) throw err;
        await new Promise(r => setTimeout(r, 1000));
      }
    }
  } else {
    // Local environment: spin up in-memory MongoDB
    const { MongoMemoryServer } = require('mongodb-memory-server');
    mongod = await MongoMemoryServer.create();
    await mongoose.connect(mongod.getUri());
  }

  // Wait until the connection is fully open before returning
  if (mongoose.connection.readyState !== 1) {
    await new Promise((resolve, reject) => {
      mongoose.connection.once('connected', resolve);
      mongoose.connection.once('error', reject);
    });
  }
}

async function disconnect() {
  if (mongoose.connection.readyState !== 0) {
    await mongoose.connection.dropDatabase();
    await mongoose.connection.close();
  }
  if (mongod) {
    await mongod.stop();
  }
}

async function clearCollections() {
  const collections = mongoose.connection.collections;
  for (const key in collections) {
    await collections[key].deleteMany({});
  }
}

module.exports = { connect, disconnect, clearCollections };

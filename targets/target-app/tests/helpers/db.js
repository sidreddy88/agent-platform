const mongoose = require('mongoose');

let mongod;

async function connect() {
  if (process.env.MONGO_URI) {
    // Docker environment: connect to real MongoDB container
    await mongoose.connect(process.env.MONGO_URI);
  } else {
    // Local environment: spin up in-memory MongoDB
    const { MongoMemoryServer } = require('mongodb-memory-server');
    mongod = await MongoMemoryServer.create();
    await mongoose.connect(mongod.getUri());
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

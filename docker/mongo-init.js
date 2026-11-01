// MongoDB's official entrypoint runs this once on a new data volume.
const database = process.env.MONGODB_DATABASE;
const username = process.env.MONGODB_USERNAME;
const password = process.env.MONGODB_PASSWORD;
if (!database || !username || !password) {
  throw new Error('AmoreGymBro database credentials are required.');
}
db.getSiblingDB('admin').createUser({
  user: username,
  pwd: password,
  roles: [{ role: 'readWrite', db: database }],
});

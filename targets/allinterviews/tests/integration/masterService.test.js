const { connect, disconnect, clearCollections } = require('../helpers/db');

jest.mock('../../constants/index', () => ({
  INSPIRING_APP: 'inspiring',
  SHOUTOUT_APP: 'shoutout',
  CR_APP: 'cr',
  BOLDJOURNEY_APP: 'boldjourney',
  INSPIRING_SERIES_APP: 'citynationaltwo',
  CITY_NATIONAL_APP: 'national',
  SMALL_BUSINESS_APP: 'smallbusiness',
  ARTIST_OF_DAY_APP: 'artistoftheday',
  LEAD_GEN_ACTIONS: { PENDING: 'pending', MARKED_INVALID: 'invalid', MARKED_SKIPPED: 'skipped' },
}));

jest.mock('../../models/MasterInspiring', () => {
  const mongoose = require('mongoose');
  if (!mongoose.models.MasterInspiring) mongoose.model('MasterInspiring', new mongoose.Schema({ email: String }));
  return mongoose.models.MasterInspiring;
});
jest.mock('../../models/MasterShoutout', () => {
  const mongoose = require('mongoose');
  if (!mongoose.models.MasterShoutout) mongoose.model('MasterShoutout', new mongoose.Schema({ email: String }));
  return mongoose.models.MasterShoutout;
});
jest.mock('../../models/MasterCr', () => {
  const mongoose = require('mongoose');
  if (!mongoose.models.MasterCr) mongoose.model('MasterCr', new mongoose.Schema({ email: String }));
  return mongoose.models.MasterCr;
});
jest.mock('../../models/MasterBoldJourney', () => {
  const mongoose = require('mongoose');
  if (!mongoose.models.MasterBoldJourney) mongoose.model('MasterBoldJourney', new mongoose.Schema({ email: String }));
  return mongoose.models.MasterBoldJourney;
});

const mongoose = require('mongoose');
const { append } = require('../../routes/services/master-service');

beforeAll(async () => { await connect(); });
afterAll(async () => { await disconnect(); });
afterEach(async () => { await clearCollections(); });

describe('masterService.append', () => {
  it('inserts new emails', async () => {
    await append('inspiring', ['a@test.com', 'b@test.com']);
    const count = await mongoose.models.MasterInspiring.countDocuments();
    expect(count).toBe(2);
  });

  it('skips duplicate emails', async () => {
    await append('inspiring', ['a@test.com']);
    await append('inspiring', ['a@test.com', 'b@test.com']);
    const count = await mongoose.models.MasterInspiring.countDocuments();
    expect(count).toBe(2);
  });

  it('does nothing for empty array', async () => {
    await append('inspiring', []);
    const count = await mongoose.models.MasterInspiring.countDocuments();
    expect(count).toBe(0);
  });

  it('throws for invalid user type', async () => {
    await expect(append('unknown', ['a@test.com'])).rejects.toThrow();
  });

  it('inserts into correct model for shoutout', async () => {
    await append('shoutout', ['x@test.com']);
    const inspiring = await mongoose.models.MasterInspiring.countDocuments();
    const shoutout = await mongoose.models.MasterShoutout.countDocuments();
    expect(inspiring).toBe(0);
    expect(shoutout).toBe(1);
  });
});

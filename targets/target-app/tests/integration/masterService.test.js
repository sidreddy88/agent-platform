const { connect, disconnect, clearCollections } = require('../helpers/db');

jest.mock('../../constants/index', () => ({
  INSPIRING_APP: 'inspiring',
  BRAND_A_APP: 'brandA',
  CR_APP: 'cr',
  BRAND_B_APP: 'brandb',
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
jest.mock('../../models/MasterBrandA', () => {
  const mongoose = require('mongoose');
  if (!mongoose.models.MasterBrandA) mongoose.model('MasterBrandA', new mongoose.Schema({ email: String }));
  return mongoose.models.MasterBrandA;
});
jest.mock('../../models/MasterBrandC', () => {
  const mongoose = require('mongoose');
  if (!mongoose.models.MasterBrandC) mongoose.model('MasterBrandC', new mongoose.Schema({ email: String }));
  return mongoose.models.MasterBrandC;
});
jest.mock('../../models/MasterBrandB', () => {
  const mongoose = require('mongoose');
  if (!mongoose.models.MasterBrandB) mongoose.model('MasterBrandB', new mongoose.Schema({ email: String }));
  return mongoose.models.MasterBrandB;
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

  it('inserts into correct model for brandA', async () => {
    await append('brandA', ['x@test.com']);
    const inspiring = await mongoose.models.MasterInspiring.countDocuments();
    const brandA = await mongoose.models.MasterBrandA.countDocuments();
    expect(inspiring).toBe(0);
    expect(brandA).toBe(1);
  });
});

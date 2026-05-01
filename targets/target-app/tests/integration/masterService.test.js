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

const mongoose = require('mongoose');

beforeAll(async () => {
  await connect();

  const emailSchema = new mongoose.Schema({ email: String });
  if (!mongoose.models.MasterInspiring) mongoose.model('MasterInspiring', emailSchema);
  if (!mongoose.models.MasterBrandA) mongoose.model('MasterBrandA', emailSchema);
  if (!mongoose.models.MasterBrandC) mongoose.model('MasterBrandC', emailSchema);
  if (!mongoose.models.MasterBrandB) mongoose.model('MasterBrandB', emailSchema);
});

afterAll(async () => {
  await disconnect();
});

afterEach(async () => {
  await clearCollections();
});

jest.mock('../../models/MasterInspiring', () => mongoose.models.MasterInspiring || mongoose.model('MasterInspiring', new mongoose.Schema({ email: String })));
jest.mock('../../models/MasterBrandA', () => mongoose.models.MasterBrandA || mongoose.model('MasterBrandA', new mongoose.Schema({ email: String })));
jest.mock('../../models/MasterBrandC', () => mongoose.models.MasterBrandC || mongoose.model('MasterBrandC', new mongoose.Schema({ email: String })));
jest.mock('../../models/MasterBrandB', () => mongoose.models.MasterBrandB || mongoose.model('MasterBrandB', new mongoose.Schema({ email: String })));

const { append } = require('../../routes/services/master-service');

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
    await expect(append('unknown', ['a@test.com'])).rejects.toThrow('Invalid user type provided.');
  });

  it('inserts into correct model for brandA', async () => {
    await append('brandA', ['x@test.com']);
    const inspiring = await mongoose.models.MasterInspiring.countDocuments();
    const brandA = await mongoose.models.MasterBrandA.countDocuments();
    expect(inspiring).toBe(0);
    expect(brandA).toBe(1);
  });
});

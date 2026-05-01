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

jest.mock('../../models/TrashReferral', () => {
  const mongoose = require('mongoose');
  if (!mongoose.models.TrashReferral) mongoose.model('TrashReferral', new mongoose.Schema({ nameOrEmail: String, type: String }));
  return mongoose.models.TrashReferral;
});
jest.mock('../../models/DoNotContactReferral', () => {
  const mongoose = require('mongoose');
  if (!mongoose.models.DoNotContactReferral) mongoose.model('DoNotContactReferral', new mongoose.Schema({ email: String }));
  return mongoose.models.DoNotContactReferral;
});
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
jest.mock('../../models/MasterCityNationalTwo', () => {
  const mongoose = require('mongoose');
  if (!mongoose.models.MasterCityNationalTwo) mongoose.model('MasterCityNationalTwo', new mongoose.Schema({ email: String }));
  return mongoose.models.MasterCityNationalTwo;
});
jest.mock('../../models/InspiringReferralUser', () => {
  const mongoose = require('mongoose');
  if (!mongoose.models.InspiringReferralUser) mongoose.model('InspiringReferralUser', new mongoose.Schema({ email: String, isLeadGen: Boolean, assignedToLeadGen: String, actionTaken: String, createdAt: Date }));
  return mongoose.models.InspiringReferralUser;
});
jest.mock('../../models/BrandAReferralUser', () => {
  const mongoose = require('mongoose');
  if (!mongoose.models.BrandAReferralUser) mongoose.model('BrandAReferralUser', new mongoose.Schema({ email: String }));
  return mongoose.models.BrandAReferralUser;
});
jest.mock('../../models/CRReferralUser', () => {
  const mongoose = require('mongoose');
  if (!mongoose.models.CRReferralUser) mongoose.model('CRReferralUser', new mongoose.Schema({ email: String }));
  return mongoose.models.CRReferralUser;
});
jest.mock('../../models/BrandBReferralUser', () => {
  const mongoose = require('mongoose');
  if (!mongoose.models.BrandBReferralUser) mongoose.model('BrandBReferralUser', new mongoose.Schema({ email: String }));
  return mongoose.models.BrandBReferralUser;
});
jest.mock('../../models/CityNationalTwoReferralUser', () => {
  const mongoose = require('mongoose');
  if (!mongoose.models.CityNationalTwoReferralUser) mongoose.model('CityNationalTwoReferralUser', new mongoose.Schema({ email: String }));
  return mongoose.models.CityNationalTwoReferralUser;
});

const mongoose = require('mongoose');
const { isTrashReferral, isDoNotContactReferral, isDuplicateReferral } = require('../../routes/services/referral-service');

beforeAll(async () => { await connect(); });
afterAll(async () => { await disconnect(); });
afterEach(async () => { await clearCollections(); });

describe('isTrashReferral', () => {
  it('returns false when email and name are not in trash list', async () => {
    const result = await isTrashReferral('clean@test.com', 'John Smith');
    expect(result).toBe(false);
  });

  it('returns true when email is in trash list', async () => {
    await mongoose.models.TrashReferral.create({ nameOrEmail: 'trash@test.com', type: 'email' });
    const result = await isTrashReferral('trash@test.com', 'John Smith');
    expect(result).toBe(true);
  });

  it('returns true when name is in trash list', async () => {
    await mongoose.models.TrashReferral.create({ nameOrEmail: 'Spam Name', type: 'name' });
    const result = await isTrashReferral('clean@test.com', 'Spam Name');
    expect(result).toBe(true);
  });

  it('handles empty name', async () => {
    const result = await isTrashReferral('clean@test.com', '');
    expect(result).toBe(false);
  });
});

describe('isDoNotContactReferral', () => {
  it('returns false for email not in do-not-contact list', async () => {
    const result = await isDoNotContactReferral('ok@test.com');
    expect(result).toBe(false);
  });

  it('returns true for email in do-not-contact list', async () => {
    await mongoose.models.DoNotContactReferral.create({ email: 'dnc@test.com' });
    const result = await isDoNotContactReferral('dnc@test.com');
    expect(result).toBe(true);
  });
});

describe('isDuplicateReferral', () => {
  it('returns false when email is not in master list', async () => {
    const result = await isDuplicateReferral('inspiring', 'new@test.com');
    expect(result).toBe(false);
  });

  it('returns true when email already exists in master list', async () => {
    await mongoose.models.MasterInspiring.create({ email: 'existing@test.com' });
    const result = await isDuplicateReferral('inspiring', 'existing@test.com');
    expect(result).toBe(true);
  });
});

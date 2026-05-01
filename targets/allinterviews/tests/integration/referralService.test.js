const { connect, disconnect, clearCollections } = require('../helpers/db');
const mongoose = require('mongoose');

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

const trashSchema = new mongoose.Schema({ nameOrEmail: String, type: String });
const doNotContactSchema = new mongoose.Schema({ email: String });
const masterSchema = new mongoose.Schema({ email: String });

jest.mock('../../models/TrashReferral', () => {
  if (!mongoose.models.TrashReferral) mongoose.model('TrashReferral', new mongoose.Schema({ nameOrEmail: String, type: String }));
  return mongoose.models.TrashReferral;
});
jest.mock('../../models/DoNotContactReferral', () => {
  if (!mongoose.models.DoNotContactReferral) mongoose.model('DoNotContactReferral', new mongoose.Schema({ email: String }));
  return mongoose.models.DoNotContactReferral;
});
jest.mock('../../models/MasterInspiring', () => {
  if (!mongoose.models.MasterInspiring) mongoose.model('MasterInspiring', new mongoose.Schema({ email: String }));
  return mongoose.models.MasterInspiring;
});
jest.mock('../../models/MasterShoutout', () => {
  if (!mongoose.models.MasterShoutout) mongoose.model('MasterShoutout', new mongoose.Schema({ email: String }));
  return mongoose.models.MasterShoutout;
});
jest.mock('../../models/MasterCr', () => {
  if (!mongoose.models.MasterCr) mongoose.model('MasterCr', new mongoose.Schema({ email: String }));
  return mongoose.models.MasterCr;
});
jest.mock('../../models/MasterBoldJourney', () => {
  if (!mongoose.models.MasterBoldJourney) mongoose.model('MasterBoldJourney', new mongoose.Schema({ email: String }));
  return mongoose.models.MasterBoldJourney;
});
jest.mock('../../models/MasterCityNationalTwo', () => {
  if (!mongoose.models.MasterCityNationalTwo) mongoose.model('MasterCityNationalTwo', new mongoose.Schema({ email: String }));
  return mongoose.models.MasterCityNationalTwo;
});
jest.mock('../../models/InspiringReferralUser', () => {
  if (!mongoose.models.InspiringReferralUser) mongoose.model('InspiringReferralUser', new mongoose.Schema({ email: String, isLeadGen: Boolean, assignedToLeadGen: String, actionTaken: String, createdAt: Date }));
  return mongoose.models.InspiringReferralUser;
});
jest.mock('../../models/ShoutoutReferralUser', () => {
  if (!mongoose.models.ShoutoutReferralUser) mongoose.model('ShoutoutReferralUser', new mongoose.Schema({ email: String }));
  return mongoose.models.ShoutoutReferralUser;
});
jest.mock('../../models/CRReferralUser', () => {
  if (!mongoose.models.CRReferralUser) mongoose.model('CRReferralUser', new mongoose.Schema({ email: String }));
  return mongoose.models.CRReferralUser;
});
jest.mock('../../models/BoldJourneyReferralUser', () => {
  if (!mongoose.models.BoldJourneyReferralUser) mongoose.model('BoldJourneyReferralUser', new mongoose.Schema({ email: String }));
  return mongoose.models.BoldJourneyReferralUser;
});
jest.mock('../../models/CityNationalTwoReferralUser', () => {
  if (!mongoose.models.CityNationalTwoReferralUser) mongoose.model('CityNationalTwoReferralUser', new mongoose.Schema({ email: String }));
  return mongoose.models.CityNationalTwoReferralUser;
});

beforeAll(async () => {
  await connect();
});

afterAll(async () => {
  await disconnect();
});

afterEach(async () => {
  await clearCollections();
});

const { isTrashReferral, isDoNotContactReferral, isDuplicateReferral } = require('../../routes/services/referral-service');

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

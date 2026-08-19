const { moderateText, classifyFields } = require('../../constants/prankCheckerOpenAI');

jest.mock('../../constants/prankCheckerOpenAI', () => ({
  moderateText: jest.fn(),
  classifyFields: jest.fn(),
}));
jest.mock('../../models/PrankCheckerLog', () => ({
  create: jest.fn().mockResolvedValue({}),
}));

const { runPrankChecker } = require('../../constants/prankCheckerMain');

const CLEAN_MODERATION = {
  moderation: { results: [{ flagged: false }] },
  tokensUsed: 10,
  costUsd: 0.001,
};
const ALLOW_CLASSIFICATION = {
  classification: { publish_decision: 'allow', risk_score: 5, primary_reason: 'clean' },
  tokensUsed: 50,
  costUsd: 0.005,
};

beforeEach(() => {
  jest.clearAllMocks();
  moderateText.mockResolvedValue(CLEAN_MODERATION);
  classifyFields.mockResolvedValue(ALLOW_CLASSIFICATION);
});

describe('runPrankChecker — clean submissions', () => {
  it('returns ok:true for clean professional text', async () => {
    const result = await runPrankChecker('I am a software engineer based in Austin, TX.');
    expect(result.ok).toBe(true);
  });

  it('calls moderateText once', async () => {
    await runPrankChecker('Clean text here.');
    expect(moderateText).toHaveBeenCalledTimes(1);
  });

  it('calls classifyFields once', async () => {
    await runPrankChecker('Clean text here.');
    expect(classifyFields).toHaveBeenCalledTimes(1);
  });
});

describe('runPrankChecker — hard block words', () => {
  it('still calls OpenAI (hard block is non-blocking in current implementation)', async () => {
    const result = await runPrankChecker('I am a horse riding enthusiast from texas');
    expect(moderateText).toHaveBeenCalled();
  });
});

describe('runPrankChecker — OpenAI moderation flagged', () => {
  it('returns ok:false when moderation flags content', async () => {
    moderateText.mockResolvedValue({
      moderation: { results: [{ flagged: true }] },
      tokensUsed: 10,
      costUsd: 0.001,
    });
    const result = await runPrankChecker('Some flagged content');
    expect(result.ok).toBe(false);
  });
});

describe('runPrankChecker — classification returns block', () => {
  it('returns ok:false when classification says block', async () => {
    classifyFields.mockResolvedValue({
      classification: { publish_decision: 'block', risk_score: 90, primary_reason: 'brand_attack', text: 'brand_attack' },
      tokensUsed: 50,
      costUsd: 0.005,
    });
    const result = await runPrankChecker('Some brand attack content');
    expect(result.ok).toBe(false);
    expect(result.errors).toBeDefined();
    expect(result.errors.length).toBeGreaterThan(0);
  });
});

describe('runPrankChecker — classifyFields fails', () => {
  it('returns ok:false when classification returns null', async () => {
    classifyFields.mockResolvedValue({ classification: null, tokensUsed: 0, costUsd: 0 });
    const result = await runPrankChecker('Some text');
    expect(result.ok).toBe(false);
    expect(result.error).toBe('OpenAI classification failed');
  });
});

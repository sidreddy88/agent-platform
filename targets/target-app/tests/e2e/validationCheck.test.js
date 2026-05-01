const { moderateText, classifyFields } = require('../../constants/validationOpenAI');

jest.mock('../../constants/validationOpenAI', () => ({
  moderateText: jest.fn(),
  classifyFields: jest.fn(),
}));
jest.mock('../../models/ValidationLog', () => ({
  create: jest.fn().mockResolvedValue({}),
}));

const { runValidationCheck } = require('../../constants/validationMain');

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
  moderateText.mockResolvedValue(CLEAN_MODERATION);
  classifyFields.mockResolvedValue(ALLOW_CLASSIFICATION);
});

describe('runValidationCheck — clean submissions', () => {
  it('returns ok:true for clean professional text', async () => {
    const result = await runValidationCheck('I am a software engineer based in Austin, TX.');
    expect(result.ok).toBe(true);
  });

  it('calls moderateText once', async () => {
    await runValidationCheck('Clean text here.');
    expect(moderateText).toHaveBeenCalledTimes(1);
  });

  it('calls classifyFields once', async () => {
    await runValidationCheck('Clean text here.');
    expect(classifyFields).toHaveBeenCalledTimes(1);
  });
});

describe('runValidationCheck — hard block words', () => {
  it('still calls OpenAI (hard block is non-blocking in current implementation)', async () => {
    const result = await runValidationCheck('I am a horse riding enthusiast from texas');
    expect(moderateText).toHaveBeenCalled();
  });
});

describe('runValidationCheck — OpenAI moderation flagged', () => {
  it('returns ok:false when moderation flags content', async () => {
    moderateText.mockResolvedValue({
      moderation: { results: [{ flagged: true }] },
      tokensUsed: 10,
      costUsd: 0.001,
    });
    const result = await runValidationCheck('Some flagged content');
    expect(result.ok).toBe(false);
  });
});

describe('runValidationCheck — classification returns block', () => {
  it('returns ok:false when classification says block', async () => {
    classifyFields.mockResolvedValue({
      classification: { publish_decision: 'block', risk_score: 90, primary_reason: 'brand_attack', text: 'brand_attack' },
      tokensUsed: 50,
      costUsd: 0.005,
    });
    const result = await runValidationCheck('Some brand attack content');
    expect(result.ok).toBe(false);
    expect(result.errors).toBeDefined();
    expect(result.errors.length).toBeGreaterThan(0);
  });
});

describe('runValidationCheck — classifyFields fails', () => {
  it('returns ok:false when classification returns null', async () => {
    classifyFields.mockResolvedValue({ classification: null, tokensUsed: 0, costUsd: 0 });
    const result = await runValidationCheck('Some text');
    expect(result.ok).toBe(false);
    expect(result.error).toBe('OpenAI classification failed');
  });
});

jest.mock('../../constants/validationOpenAI', () => ({
  moderateText: jest.fn(),
  classifyFields: jest.fn(),
}));
jest.mock('../../models/ValidationLog', () => ({
  create: jest.fn().mockResolvedValue({}),
}));

const { hardBlock, looksLikeFlagged } = require('../../constants/validationMain');

describe('hardBlock', () => {
  it('returns false for null/empty input', () => {
    expect(hardBlock(null)).toBe(false);
    expect(hardBlock('')).toBe(false);
  });

  it('returns false for clean text', () => {
    expect(hardBlock('I run a small bakery in Los Angeles.')).toBe(false);
    expect(hardBlock('My passion is helping others succeed.')).toBe(false);
  });

  it('detects hard block words surrounded by spaces', () => {
    const result = hardBlock('this is a fuck test');
    expect(result).toBeFalsy();
    const result2 = hardBlock('I am an idiot here');
    expect(result2).toBe('idiot');
  });

  it('does not flag word as substring of longer word', () => {
    expect(hardBlock('assessment of the situation')).toBe(false);
  });

  it('detects phrase "i am a horse"', () => {
    expect(hardBlock('well i am a horse right')).toBe('i am a horse');
  });

  it('is case-insensitive', () => {
    const result = hardBlock('I AM A HORSE and proud');
    expect(result).toBe('i am a horse');
  });
});

describe('looksLikeFlagged', () => {
  it('returns false for null/empty input', () => {
    expect(looksLikeFlagged(null)).toBe(false);
    expect(looksLikeFlagged('')).toBe(false);
  });

  it('returns false for clean professional text', () => {
    expect(looksLikeFlagged('I am a software engineer with 10 years of experience.')).toBe(false);
  });

  it('detects note words like "edit"', () => {
    const result = looksLikeFlagged('please edit this section carefully');
    expect(result).toBe('edit');
  });

  it('detects note phrases like "let me know"', () => {
    const result = looksLikeFlagged('just let me know if you need anything');
    expect(result).toBe('let me know');
  });

  it('detects "feel free" phrase', () => {
    const result = looksLikeFlagged('please feel free to contact me');
    expect(result).toBe('feel free');
  });

  it('detects "do not print" phrase', () => {
    const result = looksLikeFlagged('do not print this section');
    expect(result).toBe('do not print');
  });

  it('detects flagged words', () => {
    const result = looksLikeFlagged('this text contains fake content');
    expect(result).toBe('fake');
  });

  it('is case-insensitive', () => {
    const result = looksLikeFlagged('Please EDIT this for me');
    expect(result).toBe('edit');
  });
});

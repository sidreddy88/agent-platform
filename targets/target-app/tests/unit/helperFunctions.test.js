const {
  isPastNDays,
  convertToTitleCase,
  checkIfStringOnlyNA,
  containsNAVariation,
} = require('../../constants/helperFunctions');

describe('isPastNDays', () => {
  it('returns false for null date', () => {
    expect(isPastNDays(null, 7)).toBe(false);
  });

  it('returns false for undefined date', () => {
    expect(isPastNDays(undefined, 7)).toBe(false);
  });

  it('returns true for date more than N days ago', () => {
    const old = new Date(Date.now() - 10 * 24 * 60 * 60 * 1000);
    expect(isPastNDays(old, 7)).toBe(true);
  });

  it('returns false for date less than N days ago', () => {
    const recent = new Date(Date.now() - 2 * 24 * 60 * 60 * 1000);
    expect(isPastNDays(recent, 7)).toBe(false);
  });
});

describe('convertToTitleCase', () => {
  it('capitalizes first letter of each word', () => {
    expect(convertToTitleCase('hello world')).toBe('Hello World');
  });

  it('handles hyphenated words', () => {
    expect(convertToTitleCase('mary-jane watson')).toBe('Mary-Jane Watson');
  });

  it('handles single word', () => {
    expect(convertToTitleCase('john')).toBe('John');
  });

  it('preserves existing caps in each part', () => {
    expect(convertToTitleCase('abc def')).toBe('Abc Def');
  });
});

describe('checkIfStringOnlyNA', () => {
  it('returns true for NA variations', () => {
    expect(checkIfStringOnlyNA('NA')).toBe(true);
    expect(checkIfStringOnlyNA('N/A')).toBe(true);
    expect(checkIfStringOnlyNA('na')).toBe(true);
    expect(checkIfStringOnlyNA('n/a')).toBe(true);
  });

  it('returns false for non-NA strings', () => {
    expect(checkIfStringOnlyNA('Hello')).toBe(false);
    expect(checkIfStringOnlyNA('not applicable')).toBe(false);
  });

  it('returns false for null/undefined/empty', () => {
    expect(checkIfStringOnlyNA(null)).toBe(false);
    expect(checkIfStringOnlyNA(undefined)).toBe(false);
    expect(checkIfStringOnlyNA('')).toBe(false);
  });

  it('trims whitespace before checking', () => {
    expect(checkIfStringOnlyNA('  NA  ')).toBe(true);
  });
});

describe('containsNAVariation', () => {
  it('returns true when string contains NA variation', () => {
    expect(containsNAVariation('My answer is NA here')).toBe(true);
  });

  it('returns false when string has no NA variation', () => {
    expect(containsNAVariation('Hello world')).toBe(false);
  });

  it('returns false for falsy input', () => {
    expect(containsNAVariation('')).toBe(false);
    expect(containsNAVariation(null)).toBe(false);
  });
});

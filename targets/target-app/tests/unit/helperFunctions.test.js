const {
  isPastNDays,
  convertToTitleCase,
  isOnlyHtmlTags,
  checkIfStringOnlyNA,
  containsNAVariation,
  checkIfAnswerIsEmpty,
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

describe('isOnlyHtmlTags', () => {
  it('returns true for a string of only HTML tags', () => {
    expect(isOnlyHtmlTags('<p></p>')).toBe(true);
    expect(isOnlyHtmlTags('<br/><br/>')).toBe(true);
  });

  it('returns false for string with real content', () => {
    expect(isOnlyHtmlTags('<p>Hello</p>')).toBe(false);
    expect(isOnlyHtmlTags('Hello world')).toBe(false);
  });

  it('returns false for empty string', () => {
    expect(isOnlyHtmlTags('')).toBe(false);
  });

  it('returns false for whitespace only', () => {
    expect(isOnlyHtmlTags('   ')).toBe(false);
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

describe('checkIfAnswerIsEmpty', () => {
  it('returns true for null/undefined', () => {
    expect(checkIfAnswerIsEmpty(null)).toBe(true);
    expect(checkIfAnswerIsEmpty(undefined)).toBe(true);
  });

  it('returns true for string of only dots', () => {
    expect(checkIfAnswerIsEmpty('...')).toBe(true);
  });

  it('returns true for empty string after trim', () => {
    expect(checkIfAnswerIsEmpty('   ')).toBe(true);
  });

  it('returns false for real content', () => {
    expect(checkIfAnswerIsEmpty('This is a real answer.')).toBe(false);
  });

  it('returns false for answer with paragraph content', () => {
    expect(checkIfAnswerIsEmpty('Hello\n\nWorld')).toBe(false);
  });
});

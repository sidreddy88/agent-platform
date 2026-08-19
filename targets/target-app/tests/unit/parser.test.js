const parser = require('../../routes/services/parser');

describe('parser', () => {
  it('returns no email for null input', () => {
    const result = parser(null);
    expect(result.email).toBeFalsy();
    expect(result.instagram).toBeFalsy();
  });

  it('returns no email for non-string input', () => {
    const result = parser(12345);
    expect(result.email).toBeFalsy();
    expect(result.instagram).toBeFalsy();
  });

  it('extracts gmail address', () => {
    const result = parser('Contact me at john.doe@gmail.com for more info');
    expect(result.email).toBe('john.doe@gmail.com');
  });

  it('extracts yahoo address', () => {
    const result = parser('Reach me at jane@yahoo.com anytime');
    expect(result.email).toBe('jane@yahoo.com');
  });

  it('extracts plain email address', () => {
    const result = parser('Send to user@hotmail.com for details');
    expect(result.email).toBe('user@hotmail.com');
  });

  it('extracts instagram handle from URL', () => {
    const result = parser('Follow me at https://www.instagram.com/myhandle');
    expect(result.instagram).toBe('myhandle');
  });

  it('extracts instagram handle from @mention when no email present', () => {
    const result = parser('Follow me @myhandle for updates');
    expect(result.instagram).toBe('myhandle');
  });

  it('prefers email over instagram when both are present', () => {
    const result = parser('Email john@gmail.com or follow @myhandle');
    expect(result.email).toBe('john@gmail.com');
    expect(result.instagram).toBe('');
  });

  it('handles newline-separated input', () => {
    const result = parser('John Smith\njohn@gmail.com\n@johnsmith');
    expect(result.email).toBe('john@gmail.com');
  });

  it('strips trailing slash from instagram handle', () => {
    const result = parser('https://instagram.com/myhandle/');
    expect(result.instagram).toBe('myhandle');
  });
});

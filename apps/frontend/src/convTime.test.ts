import { describe, it, expect } from 'vitest';
import { timeAgo, conversationDuration, isFresh } from './convTime';

const NOW = Date.parse('2026-06-06T12:00:00Z');

describe('timeAgo', () => {
  it('returns "just now" within 10s', () => {
    expect(timeAgo('2026-06-06T11:59:55Z', NOW)).toBe('just now');
  });

  it('formats minutes', () => {
    expect(timeAgo('2026-06-06T11:55:00Z', NOW)).toBe('5m ago');
  });

  it('formats hours', () => {
    expect(timeAgo('2026-06-06T09:00:00Z', NOW)).toBe('3h ago');
  });

  it('formats days', () => {
    expect(timeAgo('2026-06-04T12:00:00Z', NOW)).toBe('2d ago');
  });

  it('returns empty string for null / unparseable', () => {
    expect(timeAgo(null, NOW)).toBe('');
    expect(timeAgo('not-a-date', NOW)).toBe('');
  });
});

describe('conversationDuration', () => {
  it('labels an ended conversation with its length', () => {
    expect(
      conversationDuration('2026-06-06T11:55:00Z', '2026-06-06T12:00:00Z', NOW),
    ).toBe('lasted 5m');
  });

  it('labels an active conversation as live with elapsed-so-far', () => {
    expect(conversationDuration('2026-06-06T11:58:00Z', null, NOW)).toBe('live · 2m');
  });

  it('uses seconds for short conversations', () => {
    expect(
      conversationDuration('2026-06-06T11:59:30Z', '2026-06-06T11:59:45Z', NOW),
    ).toBe('lasted 15s');
  });

  it('returns empty string when start is missing', () => {
    expect(conversationDuration(null, null, NOW)).toBe('');
  });
});

describe('isFresh', () => {
  it('accepts timestamps within the last 48 h', () => {
    expect(isFresh('2026-06-06T11:00:00Z', NOW)).toBe(true);
    expect(isFresh('2026-06-04T12:00:00Z', NOW)).toBe(true);
  });

  it('rejects old, future, missing and invalid timestamps', () => {
    expect(isFresh('2026-06-04T11:59:00Z', NOW)).toBe(false);
    expect(isFresh('2026-02-18T12:00:00Z', NOW)).toBe(false);
    expect(isFresh('2026-06-06T12:01:00Z', NOW)).toBe(false);
    expect(isFresh(null, NOW)).toBe(false);
    expect(isFresh(undefined, NOW)).toBe(false);
    expect(isFresh('not a date', NOW)).toBe(false);
  });
});

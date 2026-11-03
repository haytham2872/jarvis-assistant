import { EMPTY, parseSettings } from './settings';

jest.mock('expo-secure-store', () => ({}));

describe('settings', () => {
  it('reads what was saved and ignores junk', () => {
    expect(parseSettings(JSON.stringify({ baseUrl: 'https://h', token: 't', relayUrl: 'http://old' })))
      .toEqual({ baseUrl: 'https://h', token: 't' });   // a relay saved before 15 October is dropped
    expect(parseSettings(null)).toEqual(EMPTY);
    expect(parseSettings('{not json')).toEqual(EMPTY);
    expect(parseSettings(JSON.stringify({ baseUrl: 42 }))).toEqual(EMPTY);
  });
});

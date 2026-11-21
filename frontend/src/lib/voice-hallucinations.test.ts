import { describe, expect, it } from 'vitest';
import { foldAccents, isWhisperHallucination } from './voice-hallucinations';

describe('isWhisperHallucination', () => {
  it.each([
    "Sous-titres réalisés par la communauté d'Amara.org",
    'Sous-titrage ST\' 501',
    'Sous-titrage Société Radio-Canada',
    "Merci d'avoir regardé cette vidéo !",
    'Merci d’avoir regardé.',
    "N'oubliez pas de vous abonner.",
    ' ... Sous-titres',
    'Abonnez-vous !',
    'À la prochaine vidéo',
    'Thanks for watching!',
  ])('drops %j', (text) => {
    expect(isWhisperHallucination(text)).toBe(true);
  });

  it.each([
    'Bonjour Jarvis, quelle heure est-il ?',
    'Active les sous-titres sur la télé',
    'Passe à la prochaine vidéo',
    'Mets le sous-titrage stp',
    'Je voudrais regarder un film',
    "Est-ce que je dois m'abonner à ce service ?",
    '',
  ])('keeps %j', (text) => {
    expect(isWhisperHallucination(text)).toBe(false);
  });

  it('tolerates null and undefined', () => {
    expect(isWhisperHallucination(null)).toBe(false);
    expect(isWhisperHallucination(undefined)).toBe(false);
  });
});

describe('foldAccents', () => {
  it('lower-cases and strips accents', () => {
    expect(foldAccents('Réalisés À Noël')).toBe('realises a noel');
  });
});
